-- Goldmine task queue: works through the trader's plan (Tasks.lua, written by `goldmine.py push`).
--
-- The plan was made from hourly snapshots, so every price is checked live here before the
-- button that acts on it is enabled:
--   * buy: commodities are quoted (StartCommoditiesPurchase) and Confirm is enabled only if the
--     quote is <= quantity x max_unit; for item auctions only listings with buyout <= max_unit
--     can be bought. Partial fills are fine.
--   * post: price = clamp(lowest live listing that isn't yours - undercut, floor, ceil); nothing
--     is posted (status below_floor) when the live low is under the floor.
--
-- One click is one protected action. Every AH, craft and mail call happens in a button's OnClick
-- (a hardware event), one call per click; event handlers only read results and update the window.
-- Paper plans show but never enable action buttons, and expired plans are ignored.
--
-- Outcomes go to GoldmineDB.taskResults (uploaded by the client), one entry per plan:task, with
-- the live price seen. Skipping asks why; that answer is how the trader learns.

local _, ns = ...

local PRESETS = { "too expensive", "no time", "out of bag space", "don't trust it", "other" }
local FINAL = { done = true, skipped = true, blocked = true, below_floor = true, expired = true }
local AH_KINDS = { buy = true, post = true, cancel = true, scan = true }
local DURATIONS = { [12] = 1, [24] = 2, [48] = 3 } -- hours -> Enum.AuctionHouseTimeLeftBand-style index
local SILVER = 100          -- retail AH prices are whole silver
local ERROR_GRACE = 5       -- seconds an AH error can undo the click before it
local MAX_ATTACHMENTS = ATTACHMENTS_MAX_SEND or 12
local ROWS = 10

local Q = { plan = nil, tasks = {}, byId = {}, state = {}, ctx = {}, offset = 0 }
ns.TaskQueue = Q

local function Safe(fn, ...)
    if type(fn) ~= "function" then return nil end
    local ok, a, b, c = pcall(fn, ...)
    if ok then return a, b, c end
end

local function Money(c)
    return c and ns.FormatMoney(c) or "?"
end

local function ItemName(id)
    if not id then return "?" end
    local name = C_Item and Safe(C_Item.GetItemNameByID, id)
    return name or Safe(GetItemInfo, id) or ("item " .. id)
end

local function SellPrice(item)
    local ok, price = pcall(function() return select(11, GetItemInfo(item)) end)
    return ok and price or nil
end

local function Say(msg)
    ns.Print("tasks: " .. msg)
end

---------------------------------------------------------------------------
-- Plan and results
---------------------------------------------------------------------------

local resultIndex = {}

local function Results()
    ns.db.taskResults = ns.db.taskResults or {}
    return ns.db.taskResults
end

local function ResultId(t)
    return Q.plan.plan .. ":" .. t.id
end

function Q.Result(t)
    local i = resultIndex[ResultId(t)]
    return i and Results()[i]
end

function Q.Expired()
    return Q.plan ~= nil and (Q.plan.expires or 0) <= time()
end

-- Write (or update) the task's result. fields: qty, seen, paid, reason.
function Q.Record(t, status, fields)
    fields = fields or {}
    local st = Q.state[t.id]
    local r = Q.Result(t)
    if not r then
        local list = Results()
        r = { id = ResultId(t), plan = Q.plan.plan, task = t.id }
        list[#list + 1] = r
        resultIndex[r.id] = #list
    end
    r.c, r.t, r.status = ns.charKey, time(), status
    r.qty = fields.qty or (st and st.done > 0 and st.done) or r.qty
    r.paid = fields.paid or (st and st.paid > 0 and st.paid) or r.paid
    r.seen = fields.seen or (st and st.seen) or r.seen
    r.reason = fields.reason or r.reason
    if fields.closed then r.closed = true end
    return r
end

function Q.Finished(t)
    local r = Q.Result(t)
    return r ~= nil and (FINAL[r.status] or r.closed) and true or false
end

function Q.Ready(t)
    if not t.after then return true end
    local dep = Q.byId[t.after]
    local r = dep and Q.Result(dep)
    return r ~= nil and (r.status == "done" or r.status == "partial")
end

local function Remaining(t)
    return math.max((t.qty or 0) - Q.state[t.id].done, 0)
end

-- Record "expired" for every task an expired plan never got to.
local function MarkExpired()
    for _, t in ipairs(Q.tasks) do
        if not Q.Finished(t) then Q.Record(t, "expired", { closed = true }) end
    end
end

function Q.Load()
    Q.plan, Q.tasks, Q.byId, Q.state, Q.current, Q.offset = nil, {}, {}, {}, nil, 0
    Q.expiredMarked = false
    resultIndex = {}
    local p = GoldmineTasks
    if not (ns.db and type(p) == "table" and p.plan and type(p.tasks) == "table") then return end
    Q.plan = p
    -- Acknowledgement for the trader: which plan this client actually loaded, and when.
    ns.db.tasksLoaded = { plan = p.plan, generated = p.generated, t = time(), c = ns.charKey }
    for i, r in ipairs(Results()) do resultIndex[r.id] = i end
    for i, t in ipairs(p.tasks) do
        Q.tasks[i] = t
        Q.byId[t.id] = t
        local r = Q.Result(t)
        Q.state[t.id] = { done = (r and r.qty) or 0, paid = (r and r.paid) or 0, seen = r and r.seen }
    end
    if Q.Expired() then MarkExpired() end
end

local function EnsureLoaded()
    if not Q.loaded and ns.db then
        Q.loaded = true
        Q.Load()
    end
end

-- The plan is usable: loaded, not expired. (Paper plans are usable but read-only.)
local function Live()
    EnsureLoaded()
    if Q.plan and Q.Expired() then
        if not Q.expiredMarked then
            Q.expiredMarked = true
            MarkExpired()
        end
        return false
    end
    return Q.plan ~= nil
end

local function Pending(kinds)
    if not Live() then return nil end
    for _, t in ipairs(Q.tasks) do
        if not Q.Finished(t) and (not kinds or kinds[t.kind]) then return t end
    end
end

---------------------------------------------------------------------------
-- Price rules (pure; tested directly)
---------------------------------------------------------------------------

-- Post price from the lowest live listing that isn't ours. nil + "below_floor" when that listing
-- is under the floor; with nothing else listed, the ceiling.
function Q.PostPrice(low, t)
    local floor, ceil = t.floor, t.ceil
    local p = ceil
    if low then
        if low < floor then return nil, "below_floor" end
        p = math.min(math.max(low - (t.undercut or 1), floor), ceil)
    end
    p = math.floor(p / SILVER) * SILVER
    if p < floor then p = math.ceil(floor / SILVER) * SILVER end
    if p > ceil then return nil, "no_price" end -- no whole-silver price between floor and ceil
    return p
end

-- How many units to quote: what's listed at or under max_unit, up to what's still needed.
function Q.BuyQty(rows, maxUnit, remaining)
    local n = 0
    for _, r in ipairs(rows or {}) do
        if r.unit <= maxUnit then n = n + r.other end
    end
    return math.min(n, remaining)
end

function Q.QuoteOK(total, qty, maxUnit)
    return total ~= nil and qty ~= nil and qty > 0 and total <= qty * maxUnit
end

function Q.CancelMatch(unit, t)
    if not unit then return false end
    return (t.below ~= nil and unit < t.below) or (t.above ~= nil and unit > t.above)
end

local function LowestOther(rows)
    for _, r in ipairs(rows or {}) do
        if r.other > 0 then return r.unit end
    end
end

---------------------------------------------------------------------------
-- Bags
---------------------------------------------------------------------------

local function BagIndices()
    local out = {}
    for i = 0, (NUM_BAG_SLOTS or 4) do out[#out + 1] = i end
    out[#out + 1] = 5 -- reagent bag
    return out
end

-- Unlocked stacks of an item: { bag, slot, count }, and the total count.
local function Stacks(item)
    local stacks, total = {}, 0
    local cont = C_Container
    if not cont then return stacks, 0 end
    for _, bag in ipairs(BagIndices()) do
        for slot = 1, (Safe(cont.GetContainerNumSlots, bag) or 0) do
            local info = Safe(cont.GetContainerItemInfo, bag, slot)
            if info and info.itemID == item and not info.isLocked then
                stacks[#stacks + 1] = { bag = bag, slot = slot, count = info.stackCount or 1 }
                total = total + (info.stackCount or 1)
            end
        end
    end
    return stacks, total
end

---------------------------------------------------------------------------
-- Actions. Each runs from one button click and makes at most one protected call.
---------------------------------------------------------------------------

local SORTS = { { sortOrder = (Enum and Enum.AuctionHouseSortOrder and Enum.AuctionHouseSortOrder.Price) or 0,
                  reverseSort = false } }

local function Search(t)
    local st = Q.state[t.id]
    st.rows, st.market, st.quote, st.searching = nil, nil, nil, true
    C_AuctionHouse.SendSearchQuery(C_AuctionHouse.MakeItemKey(t.item), SORTS, true)
end

local function Quote(t)
    local st = Q.state[t.id]
    local qty = Q.BuyQty(st.rows, t.max_unit, Remaining(t))
    if qty <= 0 then return end
    st.quote = { qty = qty }
    C_AuctionHouse.StartCommoditiesPurchase(t.item, qty)
end

local function Confirm(t)
    local st = Q.state[t.id]
    local q = st.quote
    if not (q and Q.QuoteOK(q.total, q.qty, t.max_unit)) then return end -- re-checked at click time
    st.buying, st.quote, st.rows = q, nil, nil
    C_AuctionHouse.ConfirmCommoditiesPurchase(t.item, q.qty)
end

local function ReleaseQuote(t)
    local st = Q.state[t.id]
    st.quote, st.overLimit = nil, true
    C_AuctionHouse.CancelCommoditiesPurchase()
end

local function CheapestListing(t)
    local st = Q.state[t.id]
    for _, r in ipairs(st.rows or {}) do
        if r.other > 0 and r.auctionID and r.buyout then return r end
    end
end

local function BuyListing(t)
    local st = Q.state[t.id]
    local r = CheapestListing(t)
    if not (r and r.unit <= t.max_unit and Remaining(t) > 0) then return end
    for i, row in ipairs(st.rows) do
        if row == r then table.remove(st.rows, i) break end
    end
    st.done, st.paid = st.done + r.qty, st.paid + r.buyout
    st.undo = { at = GetTime(), done = r.qty, paid = r.buyout }
    C_AuctionHouse.PlaceBid(r.auctionID, r.buyout)
    Q.Record(t, Remaining(t) == 0 and "done" or "partial")
end

local function Post(t)
    local st = Q.state[t.id]
    local price = st.rows and Q.PostPrice(LowestOther(st.rows), t) -- re-checked at click time
    local stacks, total = Stacks(t.item)
    if not (price and stacks[1] and Remaining(t) > 0) then return end
    local loc = ItemLocation:CreateFromBagAndSlot(stacks[1].bag, stacks[1].slot)
    local status = Safe(C_AuctionHouse.GetItemCommodityStatus, loc)
    local commodity = st.market == "commodity"
    if status and Enum and Enum.ItemCommodityStatus then commodity = status == Enum.ItemCommodityStatus.Commodity end
    local duration = DURATIONS[t.duration or 24] or 2
    local n
    if commodity then
        n = math.min(Remaining(t), total)
        C_AuctionHouse.PostCommodity(loc, duration, n, price)
    else
        n = 1
        C_AuctionHouse.PostItem(loc, duration, 1, nil, price)
    end
    st.done = st.done + n
    st.undo = { at = GetTime(), done = n, paid = 0 }
    Q.Record(t, Remaining(t) == 0 and "done" or "partial")
end

local function QueryOwned(t)
    local st = Q.state[t.id]
    st.owned, st.querying = nil, true
    C_AuctionHouse.QueryOwnedAuctions({})
end

local function CancelOne(t)
    local st = Q.state[t.id]
    local a = st.owned and st.owned[1]
    if not a then return end
    table.remove(st.owned, 1)
    st.done = st.done + 1
    st.undo = { at = GetTime(), done = 1, paid = 0 }
    C_AuctionHouse.CancelAuction(a.auctionID)
    Q.Record(t, #st.owned == 0 and "done" or "partial")
end

local function OpenRecipe(t)
    local st = Q.state[t.id]
    local schematic = C_TradeSkillUI and Safe(C_TradeSkillUI.GetRecipeSchematic, t.recipe, false)
    st.output = schematic and schematic.outputItemID
    C_TradeSkillUI.OpenRecipe(t.recipe)
    -- Pre-fill the "create multiple" count; the human presses Create (Blizzard's button).
    local page = ProfessionsFrame and ProfessionsFrame.CraftingPage
    local box = page and page.CreateMultipleInputBox
    if box and box.SetValue then pcall(box.SetValue, box, math.max(Remaining(t), 1)) end
    st.opened = true
end

local function MarkCrafted(t)
    Q.Record(t, "done", { closed = true })
end

-- Next whole stack to attach/sell, if it doesn't overshoot what the task wants.
local function NextStack(t, already)
    local stacks = Stacks(t.item)
    local s = stacks[1]
    if not s then return nil, "none left in bags" end
    if t.qty then
        local want = Remaining(t) - (already or 0)
        if want <= 0 then return nil, "enough attached" end
        if s.count > want then return nil, ("split a stack: %d needed, smallest stack %d"):format(want, s.count) end
    end
    return s
end

local function Attach(t)
    local st = Q.state[t.id]
    local s = NextStack(t, st.attached)
    if not s or (st.attachedStacks or 0) >= MAX_ATTACHMENTS then return end
    if SendMailNameEditBox and (SendMailNameEditBox:GetText() or "") == "" then SendMailNameEditBox:SetText(t.to) end
    st.attached, st.attachedStacks = (st.attached or 0) + s.count, (st.attachedStacks or 0) + 1
    C_Container.UseContainerItem(s.bag, s.slot) -- with the Send Mail tab open, this attaches the stack
end

local function Sell(t)
    local st = Q.state[t.id]
    local s = NextStack(t)
    if not s then return end
    local stacks = Stacks(t.item)
    st.seen = st.seen or SellPrice(t.item)
    st.done = st.done + s.count
    C_Container.UseContainerItem(s.bag, s.slot) -- at a merchant, this sells the stack
    local more = #stacks > 1 and (not t.qty or Remaining(t) > 0)
    Q.Record(t, more and "partial" or "done")
end

---------------------------------------------------------------------------
-- What each task offers right now: up to two buttons and some lines of live info.
-- A button is { label, enabled, run }; run is called from OnClick only.
---------------------------------------------------------------------------

local function B(label, enabled, run) return { label = label, enabled = enabled and true or false, run = run } end

local function NeedAH(info)
    if not Q.ctx.ah then info[#info + 1] = "Open the auction house." return false end
    return true
end

local VIEW = {}

function VIEW.buy(t, st, info)
    info[#info + 1] = ("Buy %d more at <= %s each (limit %s)."):format(Remaining(t), Money(t.max_unit),
        Money(Remaining(t) * t.max_unit))
    if st.done > 0 then info[#info + 1] = ("Bought %d for %s so far."):format(st.done, Money(st.paid)) end
    if not NeedAH(info) then return B("Search", false) end
    if st.buying then return B(("Buying %d..."):format(st.buying.qty), false) end
    if not st.rows then return B(st.searching and "Searching..." or "Search", not st.searching, Search) end
    if st.seen then info[#info + 1] = "Live lowest: " .. Money(st.seen) end
    if st.market == "commodity" then
        local q = st.quote
        if q and q.total then
            local ok = Q.QuoteOK(q.total, q.qty, t.max_unit)
            info[#info + 1] = ("Quote: %d for %s (%s each)%s"):format(q.qty, Money(q.total), Money(q.unit),
                ok and "" or ("  |cffff4040over the %s limit|r"):format(Money(q.qty * t.max_unit)))
            if ok then return B("Search again", true, Search), B("Confirm " .. Money(q.total), true, Confirm) end
            st.overLimit = true
            return B("Search again", true, Search), B("Release quote", true, ReleaseQuote)
        elseif q then
            return B("Search again", true, Search), B("Waiting for quote...", false)
        end
        local n = Q.BuyQty(st.rows, t.max_unit, Remaining(t))
        if n == 0 then
            st.overLimit = true
            info[#info + 1] = "|cffff4040Nothing listed at or under the limit.|r"
        end
        return B("Search again", true, Search), B(n > 0 and ("Get quote for " .. n) or "Get quote", n > 0, Quote)
    end
    local r = CheapestListing(t)
    if not r then
        info[#info + 1] = "No listings."
        return B("Search again", true, Search)
    end
    local ok = r.unit <= t.max_unit
    if not ok then
        st.overLimit = true
        info[#info + 1] = ("|cffff4040Cheapest is %s, over the limit.|r"):format(Money(r.unit))
    end
    return B("Search again", true, Search), B(("Buy %d for %s"):format(r.qty, Money(r.buyout)), ok, BuyListing)
end

function VIEW.post(t, st, info)
    local _, have = Stacks(t.item)
    info[#info + 1] = ("Post %d more (%d in bags) between %s and %s, %dh."):format(Remaining(t), have,
        Money(t.floor), Money(t.ceil), t.duration or 24)
    if not NeedAH(info) then return B("Search", false) end
    if not st.rows then return B(st.searching and "Searching..." or "Search", not st.searching, Search) end
    local low = LowestOther(st.rows)
    info[#info + 1] = "Lowest other listing: " .. (low and Money(low) or "none")
    local price, why = Q.PostPrice(low, t)
    st.postPrice = price
    if not price then
        info[#info + 1] = why == "below_floor" and "|cffff4040Below your floor: not posting.|r"
            or "|cffff4040No whole-silver price between floor and ceil.|r"
        return B("Search again", true, Search)
    end
    local n = st.market == "commodity" and math.min(Remaining(t), have) or math.min(1, have)
    return B("Search again", true, Search), B(("Post %d at %s"):format(n, Money(price)), n > 0, Post)
end

function VIEW.cancel(t, st, info)
    local rule = {}
    if t.below then rule[#rule + 1] = "below " .. Money(t.below) end
    if t.above then rule[#rule + 1] = "above " .. Money(t.above) end
    info[#info + 1] = "Cancel my auctions priced " .. table.concat(rule, " or ") .. "."
    if st.done > 0 then info[#info + 1] = st.done .. " cancelled." end
    if not NeedAH(info) then return B("Find my auctions", false) end
    if not st.owned then
        return B(st.querying and "Loading..." or "Find my auctions", not st.querying, QueryOwned)
    end
    local a = st.owned[1]
    if not a then
        info[#info + 1] = "None left to cancel."
        return B("Find my auctions", true, QueryOwned)
    end
    info[#info + 1] = #st.owned .. " to cancel."
    return B("Find my auctions", true, QueryOwned), B(("Cancel 1 (%s)"):format(Money(a.unit)), true, CancelOne)
end

function VIEW.craft(t, st, info)
    info[#info + 1] = ("Craft %d (recipe %d). Opens the recipe; you press Create."):format(t.qty, t.recipe)
    if st.done > 0 then info[#info + 1] = st.done .. " crafted." end
    return B("Open recipe", true, OpenRecipe), B("Mark done", st.opened or st.done > 0, MarkCrafted)
end

function VIEW.scan(t, st, info)
    info[#info + 1] = "Search it so the server gets a live price."
    if not NeedAH(info) then return B("Search", false) end
    return B(st.searching and "Searching..." or "Search", not st.searching, Search)
end

function VIEW.mail(t, st, info)
    info[#info + 1] = ("Mail %d to %s. Attach, then press Send."):format(Remaining(t), t.to)
    if not Q.ctx.mail then info[#info + 1] = "Open a mailbox." return B("Attach stack", false) end
    if SendMailFrame and not SendMailFrame:IsShown() then
        info[#info + 1] = "Open the Send Mail tab."
        return B("Attach stack", false)
    end
    if (st.attached or 0) > 0 then info[#info + 1] = st.attached .. " attached." end
    local s, why = NextStack(t, st.attached)
    if not s then info[#info + 1] = why end
    return B("Attach stack", s and (st.attachedStacks or 0) < MAX_ATTACHMENTS, Attach)
end

function VIEW.vendor(t, st, info)
    info[#info + 1] = t.qty and ("Sell %d to a vendor."):format(Remaining(t)) or "Sell all to a vendor."
    if not Q.ctx.merchant then info[#info + 1] = "Open a merchant." return B("Sell stack", false) end
    local s, why = NextStack(t)
    if not s then info[#info + 1] = why end
    return B(s and ("Sell stack of " .. s.count) or "Sell stack", s ~= nil, Sell)
end

-- Buttons and info for a task. Paper plans and unmet dependencies disable every action.
function Q.View(t)
    local st, info = Q.state[t.id], {}
    if t.why then info[1] = "|cffaaaaaa" .. t.why .. "|r" end
    local a1, a2 = VIEW[t.kind](t, st, info)
    local block
    if Q.plan.paper then block = "Paper plan: actions are disabled." end
    if not Q.Ready(t) then block = "Waits for " .. t.after .. "." end
    if Q.Finished(t) then block = "Finished: " .. Q.Result(t).status .. "." end
    if block then
        info[#info + 1] = block
        if a1 then a1.enabled = false end
        if a2 then a2.enabled = false end
    end
    return a1, a2, info
end

function Q.Describe(t)
    local what
    if t.kind == "craft" then
        what = ("Craft %dx recipe %d"):format(t.qty, t.recipe)
    elseif t.kind == "mail" then
        what = ("Mail %d %s to %s"):format(t.qty, ItemName(t.item), t.to)
    elseif t.kind == "buy" then
        what = ("Buy %d %s <= %s"):format(t.qty, ItemName(t.item), Money(t.max_unit))
    elseif t.kind == "post" then
        what = ("Post %d %s >= %s"):format(t.qty, ItemName(t.item), Money(t.floor))
    else
        what = t.kind:sub(1, 1):upper() .. t.kind:sub(2) .. " " .. (t.qty and (t.qty .. " ") or "") .. ItemName(t.item)
    end
    local extra = ("%sm"):format(t.est_min or 0)
    if t.ev and t.ev ~= 0 then extra = extra .. ", ev " .. Money(t.ev) end
    return what, extra
end

---------------------------------------------------------------------------
-- Skipping (asks why)
---------------------------------------------------------------------------

function Q.Skip(t, reason)
    local st = Q.state[t.id]
    local status = (st.done > 0 and "partial") or (st.overLimit and "blocked") or "skipped"
    if st.quote then
        st.quote = nil
        C_AuctionHouse.CancelCommoditiesPurchase() -- the skip click releases a pending quote
    end
    Q.Record(t, status, { reason = reason, closed = true })
    Q.skipping = nil
    Q.Advance()
end

---------------------------------------------------------------------------
-- Live results (events). Nothing here calls a protected function.
---------------------------------------------------------------------------

local function Current()
    return Q.current and Q.byId[Q.current]
end

local function ForItem(item, kinds)
    local t = Current()
    if t and t.item == item and kinds[t.kind] then return t, Q.state[t.id] end
end

local SEARCH_KINDS = { buy = true, post = true, scan = true }

local function GotRows(t, st, market, rows)
    st.rows, st.market, st.searching = rows, market, false
    st.seen = LowestOther(rows)
    if t.kind == "scan" then
        Q.Record(t, "done", { seen = st.seen, reason = not st.seen and "none listed" or nil })
    elseif t.kind == "post" then
        local price, why = Q.PostPrice(st.seen, t)
        if not price and why == "below_floor" then Q.Record(t, "below_floor", { closed = true }) end
    end
end

local events = {}

function events.COMMODITY_SEARCH_RESULTS_UPDATED(itemID)
    local t, st = ForItem(itemID, SEARCH_KINDS)
    if not t then return end
    local rows = {}
    for i = 1, (Safe(C_AuctionHouse.GetNumCommoditySearchResults, itemID) or 0) do
        local r = Safe(C_AuctionHouse.GetCommoditySearchResultInfo, itemID, i)
        if r and r.unitPrice then
            local mine = r.containsOwnerItem and (r.numOwnerItems or r.quantity) or 0
            if r.containsAccountItem and not r.containsOwnerItem then mine = r.quantity end
            rows[#rows + 1] = { unit = r.unitPrice, qty = r.quantity, other = math.max(r.quantity - mine, 0) }
        end
    end
    GotRows(t, st, "commodity", rows)
end

function events.ITEM_SEARCH_RESULTS_UPDATED(itemKey)
    local t, st = ForItem(type(itemKey) == "table" and itemKey.itemID, SEARCH_KINDS)
    if not t then return end
    local rows = {}
    for i = 1, (Safe(C_AuctionHouse.GetNumItemSearchResults, itemKey) or 0) do
        local r = Safe(C_AuctionHouse.GetItemSearchResultInfo, itemKey, i)
        if r and r.buyoutAmount then
            local qty = math.max(r.quantity or 1, 1)
            local mine = r.containsOwnerItem or r.containsAccountItem
            rows[#rows + 1] = { unit = math.floor(r.buyoutAmount / qty), qty = qty, buyout = r.buyoutAmount,
                                auctionID = r.auctionID, other = mine and 0 or qty }
        end
    end
    table.sort(rows, function(a, b) return a.unit < b.unit end)
    GotRows(t, st, "item", rows)
end

function events.COMMODITY_PRICE_UPDATED(unitPrice, totalPrice)
    local t = Current()
    local st = t and Q.state[t.id]
    if st and st.quote then
        st.quote.unit, st.quote.total = unitPrice, totalPrice
        st.seen = unitPrice
    end
end

function events.COMMODITY_PRICE_UNAVAILABLE()
    local t = Current()
    if t and Q.state[t.id].quote then
        Q.state[t.id].quote = nil
        Say("no quote available; search again.")
    end
end

function events.COMMODITY_PURCHASE_SUCCEEDED()
    for _, t in ipairs(Q.tasks) do
        local st = Q.state[t.id]
        if st.buying then
            st.done, st.paid = st.done + st.buying.qty, st.paid + (st.buying.total or 0)
            st.buying, st.rows = nil, nil -- search again before buying more
            Q.Record(t, Remaining(t) == 0 and "done" or "partial")
        end
    end
end

function events.COMMODITY_PURCHASE_FAILED()
    for _, t in ipairs(Q.tasks) do
        local st = Q.state[t.id]
        if st.buying then
            st.buying, st.rows = nil, nil
            Say("purchase failed; search again.")
        end
    end
end

-- An AH error right after a click undoes what that click counted.
function events.AUCTION_HOUSE_SHOW_ERROR()
    for _, t in ipairs(Q.tasks) do
        local st = Q.state[t.id]
        local u = st.undo
        if u and GetTime() - u.at <= ERROR_GRACE then
            st.done, st.paid, st.undo = st.done - u.done, st.paid - u.paid, nil
            local r = Q.Result(t)
            if r then r.status, r.closed, r.qty, r.paid = "partial", nil, st.done, st.paid > 0 and st.paid or nil end
            Q.current = t.id -- back to it, if that click had finished it
        end
    end
end

function events.OWNER_AUCTIONS_UPDATED()
    local t = Current()
    if not (t and t.kind == "cancel") then return end
    local st = Q.state[t.id]
    local owned = {}
    for i = 1, (Safe(C_AuctionHouse.GetNumOwnedAuctions) or 0) do
        local a = Safe(C_AuctionHouse.GetOwnedAuctionInfo, i)
        -- buyoutAmount is taken as per unit (one unit per item auction; commodities report per unit).
        local unit = a and (a.buyoutAmount or a.bidAmount)
        if a and a.status == 0 and a.itemKey and a.itemKey.itemID == t.item and Q.CancelMatch(unit, t) then
            owned[#owned + 1] = { auctionID = a.auctionID, unit = unit }
        end
    end
    table.sort(owned, function(a, b) return a.unit < b.unit end)
    st.owned, st.querying = owned, false
    if owned[1] then st.seen = owned[1].unit end
    if #owned == 0 then Q.Record(t, "done", { qty = st.done }) end
end

function events.TRADE_SKILL_ITEM_CRAFTED_RESULT(result)
    for _, t in ipairs(Q.tasks) do
        local st = Q.state[t.id]
        if t.kind == "craft" and st.opened and type(result) == "table"
            and (result.recipeID == t.recipe or (st.output and result.itemID == st.output))
            and not result.isResourcefulnessReturn then
            st.done = st.done + 1
            Q.Record(t, st.done >= t.qty and "done" or "partial")
            return
        end
    end
end

function events.MAIL_SEND_SUCCESS()
    for _, t in ipairs(Q.tasks) do
        local st = Q.state[t.id]
        if t.kind == "mail" and (st.attached or 0) > 0 then
            st.done, st.attached, st.attachedStacks = st.done + st.attached, 0, 0
            Q.Record(t, (Remaining(t) == 0 or not NextStack(t)) and "done" or "partial")
        end
    end
end

function events.MAIL_FAILED()
    for _, t in ipairs(Q.tasks) do
        Q.state[t.id].attached, Q.state[t.id].attachedStacks = 0, 0
    end
end

local CONTEXTS = {
    AUCTION_HOUSE_SHOW = { "ah", true, AH_KINDS }, AUCTION_HOUSE_CLOSED = { "ah", false },
    MAIL_SHOW = { "mail", true, { mail = true } }, MAIL_CLOSED = { "mail", false },
    MERCHANT_SHOW = { "merchant", true, { vendor = true } }, MERCHANT_CLOSED = { "merchant", false },
    TRADE_SKILL_SHOW = { "craft", true, { craft = true } }, TRADE_SKILL_CLOSE = { "craft", false },
}

local function OnContext(event)
    local spec = CONTEXTS[event]
    Q.ctx[spec[1]] = spec[2]
    if spec[1] == "ah" and not spec[2] then
        for _, st in pairs(Q.state) do st.quote, st.searching, st.querying = nil, false, false end
    end
    if spec[2] then
        local t = Pending(spec[3])
        if t then
            local cur = Current()
            if not (cur and spec[3][cur.kind] and not Q.Finished(cur)) then Q.current = t.id end
            Q.Show()
        end
    end
end

---------------------------------------------------------------------------
-- Window
---------------------------------------------------------------------------

local ui

local function Button(parent, w, label)
    local b = CreateFrame("Button", nil, parent, "UIPanelButtonTemplate")
    b:SetSize(w, 22)
    b:SetText(label)
    return b
end

local function Text(parent, template)
    local fs = parent:CreateFontString(nil, "OVERLAY", template or "GameFontHighlightSmall")
    fs:SetJustifyH("LEFT")
    return fs
end

-- Clicks go through here: the button's action runs once, then the window refreshes.
local function OnAction(self)
    local t = Current()
    local spec = self.spec
    if not (t and spec and spec.enabled and spec.run) then return end
    spec.run(t)
    if Q.Finished(t) then Q.Advance() else Q.Refresh() end
end

local function BuildSkipPrompt(f)
    local p = CreateFrame("Frame", nil, f, "BackdropTemplate")
    p:SetPoint("BOTTOMLEFT", 8, 8)
    p:SetPoint("BOTTOMRIGHT", -8, 8)
    p:SetHeight(110)
    if p.SetBackdrop then
        p:SetBackdrop({ bgFile = "Interface\\Tooltips\\UI-Tooltip-Background", edgeFile = "Interface\\Tooltips\\UI-Tooltip-Border",
                        edgeSize = 12, insets = { left = 3, right = 3, top = 3, bottom = 3 } })
        p:SetBackdropColor(0, 0, 0, 0.95)
    end
    p.question = Text(p, "GameFontNormal")
    p.question:SetPoint("TOPLEFT", 10, -8)
    p.question:SetText("Why skip it?")
    p.note = CreateFrame("EditBox", nil, p, "InputBoxTemplate")
    p.note:SetSize(300, 20)
    p.note:SetAutoFocus(false)
    p.note:SetPoint("TOPLEFT", 14, -28)
    p.presets = {}
    for i, reason in ipairs(PRESETS) do
        local b = Button(p, 82, reason)
        b:SetPoint("TOPLEFT", 8 + ((i - 1) % 4) * 86, -54 - math.floor((i - 1) / 4) * 26)
        b:SetScript("OnClick", function()
            local t = Q.skipping and Q.byId[Q.skipping]
            if not t then return end
            local note = (p.note:GetText() or ""):gsub("^%s+", ""):gsub("%s+$", "")
            local why = reason
            if note ~= "" then why = (reason == "other") and note or (reason .. ": " .. note) end
            p.note:SetText("")
            p:Hide()
            Q.Skip(t, why)
        end)
        p.presets[i] = b
    end
    p.back = Button(p, 82, "Back")
    p.back:SetPoint("TOPLEFT", 8 + 86, -80)
    p.back:SetScript("OnClick", function()
        Q.skipping = nil
        p:Hide()
        Q.Refresh()
    end)
    p:Hide()
    return p
end

local function Build()
    local f = CreateFrame("Frame", "GoldmineTaskQueue", UIParent, "BasicFrameTemplateWithInset")
    f:SetSize(460, 430)
    f:SetFrameStrata("HIGH")
    f:SetClampedToScreen(true)
    f:SetMovable(true)
    f:EnableMouse(true)
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", f.StartMoving)
    f:SetScript("OnDragStop", function(self)
        self:StopMovingOrSizing()
        local point, _, rel, x, y = self:GetPoint()
        ns.db.settings.taskQueuePos = { point, rel, x, y }
    end)
    local pos = ns.db.settings.taskQueuePos
    if pos then f:SetPoint(pos[1], UIParent, pos[2], pos[3], pos[4]) else f:SetPoint("CENTER", UIParent, "CENTER", 300, 0) end
    if type(f.TitleText) == "table" then f.TitleText:SetText("Goldmine tasks") end
    if UISpecialFrames then table.insert(UISpecialFrames, "GoldmineTaskQueue") end

    f.header = Text(f, "GameFontNormal")
    f.header:SetPoint("TOPLEFT", 14, -30)
    f.header:SetWidth(430)

    f.rows = {}
    for i = 1, ROWS do
        local row = CreateFrame("Button", nil, f)
        row:SetSize(430, 16)
        row:SetPoint("TOPLEFT", 14, -48 - (i - 1) * 16)
        row.text = Text(row)
        row.text:SetPoint("LEFT")
        row.text:SetWidth(360)
        row.extra = Text(row)
        row.extra:SetPoint("RIGHT")
        row.extra:SetJustifyH("RIGHT")
        row:SetScript("OnClick", function(self)
            if self.task then
                Q.current = self.task
                Q.Refresh()
            end
        end)
        f.rows[i] = row
    end
    f:EnableMouseWheel(true)
    f:SetScript("OnMouseWheel", function(_, delta)
        Q.offset = math.max(0, math.min(Q.offset - delta, math.max(#Q.tasks - ROWS, 0)))
        Q.Refresh()
    end)

    f.title = Text(f, "GameFontNormalLarge")
    f.title:SetPoint("TOPLEFT", 14, -48 - ROWS * 16 - 10)
    f.title:SetWidth(430)
    f.info = Text(f)
    f.info:SetPoint("TOPLEFT", f.title, "BOTTOMLEFT", 0, -6)
    f.info:SetWidth(430)

    f.action1 = Button(f, 140, "")
    f.action1:SetPoint("BOTTOMLEFT", 12, 12)
    f.action2 = Button(f, 160, "")
    f.action2:SetPoint("LEFT", f.action1, "RIGHT", 6, 0)
    f.skip = Button(f, 80, "Skip...")
    f.skip:SetPoint("BOTTOMRIGHT", -12, 12)
    for _, b in ipairs({ f.action1, f.action2 }) do b:SetScript("OnClick", OnAction) end
    f.skip:SetScript("OnClick", function()
        local t = Current()
        if not t or Q.Finished(t) then return end
        Q.skipping = t.id
        f.prompt.question:SetText("Why skip: " .. (Q.Describe(t)) .. "?")
        f.prompt:Show()
    end)
    f.prompt = BuildSkipPrompt(f)
    f:Hide()
    return f
end

local function SetButton(b, spec)
    b.spec = spec
    if not spec then
        b:Hide()
        return
    end
    b:SetText(spec.label)
    if spec.enabled then b:Enable() else b:Disable() end
    b:Show()
end

local STATUS_COLORS = { done = "ff40ff40", partial = "ffffd100", skipped = "ff999999", blocked = "ffff4040",
                        below_floor = "ffff8040", expired = "ff999999" }

function Q.Refresh()
    if not (ui and ui:IsShown()) then return end
    EnsureLoaded()
    if not Q.plan then
        ui.header:SetText("No plan. The trader publishes one; `goldmine.py push` and /reload load it.")
    elseif Q.Expired() then
        ui.header:SetText(("Plan %s expired %s; waiting for a new one."):format(Q.plan.plan, date("%m-%d %H:%M", Q.plan.expires)))
    else
        local total, left = 0, 0
        for _, t in ipairs(Q.tasks) do
            total = total + (t.est_min or 0)
            if not Q.Finished(t) then left = left + (t.est_min or 0) end
        end
        ui.header:SetText(("%s%s: %s of %s min left (budget %s), until %s"):format(Q.plan.plan,
            Q.plan.paper and " |cff80c0ff[PAPER]|r" or "", left, total, Q.plan.budget_min or "?",
            date("%H:%M", Q.plan.expires)))
    end
    local live = Live()
    if live and not Current() then
        local t = Pending()
        Q.current = t and t.id
    end
    for i, row in ipairs(ui.rows) do
        local t = live and Q.tasks[i + Q.offset]
        row.task = t and t.id
        if t then
            local what, extra = Q.Describe(t)
            local r = Q.Result(t)
            local mark = (t.id == Q.current) and "> " or "  "
            local status = r and ("  |c%s%s|r"):format(STATUS_COLORS[r.status] or "ffffffff", r.status) or ""
            row.text:SetText(("%s%d. %s%s"):format(mark, i + Q.offset, what, status))
            row.extra:SetText(extra)
            row:Show()
        else
            row:Hide()
        end
    end
    local t = live and Current()
    if not t then
        ui.title:SetText(live and "All done." or "")
        ui.info:SetText("")
        SetButton(ui.action1, nil)
        SetButton(ui.action2, nil)
        ui.skip:Hide()
        return
    end
    local a1, a2, info = Q.View(t)
    ui.title:SetText((Q.Describe(t)))
    ui.info:SetText(table.concat(info, "\n"))
    SetButton(ui.action1, a1)
    SetButton(ui.action2, a2)
    if Q.Finished(t) then ui.skip:Hide() else ui.skip:Show() end
end

-- After a task finishes, move to the next unfinished one.
function Q.Advance()
    local t = Pending()
    Q.current = t and t.id or nil
    Q.Refresh()
end

function Q.Show()
    EnsureLoaded()
    ui = ui or Build()
    Q.ui = ui
    ui:Show()
    Q.Refresh()
end

function Q.Toggle()
    if ui and ui:IsShown() then ui:Hide() else Q.Show() end
end

ns.commands.tasks = function(arg)
    if arg == "reset" then -- forget this session's live state (not the recorded results)
        Q.loaded = false
        EnsureLoaded()
        Q.Refresh()
        return
    end
    Q.Toggle()
end

---------------------------------------------------------------------------

local frame = CreateFrame("Frame")
frame:SetScript("OnEvent", function(_, event, ...)
    if CONTEXTS[event] then
        EnsureLoaded()
        OnContext(event)
    elseif event == "PLAYER_ENTERING_WORLD" then
        EnsureLoaded()
    elseif events[event] then
        if not Live() then return end -- no plan, or it expired
        events[event](...)
        local t = Current()
        if t and Q.Finished(t) and not Q.skipping then Q.Advance() else Q.Refresh() end
    end
end)
for event in pairs(events) do pcall(frame.RegisterEvent, frame, event) end
for event in pairs(CONTEXTS) do pcall(frame.RegisterEvent, frame, event) end
pcall(frame.RegisterEvent, frame, "PLAYER_ENTERING_WORLD")
