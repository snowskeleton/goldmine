-- Goldmine market data: passively records what the game already shows you.
--
--   * AH prices from searches you run (commodity, item, and browse results). No price queries
--     are sent on your behalf; this only reads results the AH UI requested.
--   * Your learned recipes and their reagents (when a profession window is open), and every
--     character's professions and skill (on login), with a reminder to open a profession
--     window whose recipes are missing or more than a week old.
--   * Bag, bank, and warband bank contents, and the gold in the warband bank.
--   * Your own auctions (OWNER_AUCTIONS_UPDATED). Goldmine asks for that list when the AH opens
--     and after you post or cancel; GoldmineDB.settings.queryOwnedAuctions = false turns that off.
--   * Auction house mail in your inbox: sale invoices (buyer, price, deposit, cut), and won,
--     outbid, expired and cancelled auctions.
--   * Crafting orders the crafting orders window shows you (public, guild, personal, patron).
--   * Crafting operation info per recipe (skill, difficulty, expected quality, multicraft/
--     resourcefulness, concentration cost) and the result of every craft you make, with the
--     reagents it used up.
--   * Stack size, binding, expansion and vendor price of items you hold.
--
-- The desktop tool imports all of this; scans are keyed by id so re-importing is safe.

local _, ns = ...

local MAX_SCANS = 20000      -- cap on stored scans; oldest are dropped (sync first!)
local MAX_ROWS = 60          -- price levels kept per item search
local MERGE_WINDOW = 60      -- seconds: repeated results for the same search update one scan
local MAX_OWNED = 1000       -- owned auctions kept per snapshot
local MAX_OPS = 400          -- recipes per profession that get crafting operation info
local MAX_CRAFTS = 5000      -- stored craft results; oldest are dropped
local MAX_ITEM_INFO = 5000   -- items with cached stack size / binding / expansion
local MAX_MAIL = 5000        -- stored auction mails; oldest are dropped
local MAX_ORDERS = 2000      -- stored crafting orders; oldest are dropped
local STALE_RECIPES = 7 * 86400 -- remind to open a profession window after this long
local MAIL_MATCH = 3600      -- seconds: the same mail's estimated arrival drifts less than this

local frame = CreateFrame("Frame")
local handlers = {}
local pendingTimers = {}

local function Safe(fn, ...)
    if type(fn) ~= "function" then return nil end
    local ok, a, b, c = pcall(fn, ...)
    if ok then return a, b, c end
end

-- Remember an item's name, stack size, binding, expansion and vendor price (what the item API
-- doesn't say, or says only once the server gets round to asking), once per item. Skipped while
-- the client hasn't cached the item yet. Entries from before names were kept get one more look.
local function NoteItem(itemID)
    local db = ns.db
    if not (db and itemID) then return end
    db.itemInfo = db.itemInfo or {}
    local known = db.itemInfo[itemID]
    if known and known.n then return end
    db.itemInfoCount = db.itemInfoCount or 0
    if not known and db.itemInfoCount >= MAX_ITEM_INFO then return end
    local get = (C_Item and C_Item.GetItemInfo) or GetItemInfo
    local ok, name, _, _, _, _, _, _, stack, _, _, sell, class, subclass, bind, expansion = pcall(get, itemID)
    if not (ok and name) then return end
    db.itemInfo[itemID] = { n = name, s = stack, v = sell, c = class, sc = subclass, b = bind, x = expansion,
                            t = time() }
    if not known then db.itemInfoCount = db.itemInfoCount + 1 end
end

-- Run fn once, `delay` seconds after the last call with this key.
local function Debounce(key, delay, fn)
    local token = {}
    pendingTimers[key] = token
    C_Timer.After(delay, function()
        if pendingTimers[key] == token then
            pendingTimers[key] = nil
            fn()
        end
    end)
end

---------------------------------------------------------------------------
-- AH scans
---------------------------------------------------------------------------

-- rows is a flat array: unit_price, quantity, unit_price, quantity, ...
-- (for "browse": item_id, variant, min_price, quantity, ...)
local function SaveScan(kind, itemID, variant, rows)
    local db = ns.db
    if not (db and ns.charKey) or #rows == 0 then return end
    db.scans = db.scans or {}
    local now = time()
    local last = db.scans[#db.scans]
    if last and last.c == ns.charKey and last.kind == kind and last.item == itemID
        and last.v == variant and now - last.t <= MERGE_WINDOW then
        last.t, last.rows = now, rows -- results arrive incrementally; keep the fullest
        return
    end
    db.scanSeq = (db.scanSeq or 0) + 1
    db.scans[#db.scans + 1] = {
        id = ns.charKey .. ":s" .. db.scanSeq, c = ns.charKey, t = now,
        kind = kind, item = itemID, v = variant, rows = rows,
    }
    if #db.scans > MAX_SCANS + 1000 then
        local kept = {}
        for i = #db.scans - MAX_SCANS + 1, #db.scans do kept[#kept + 1] = db.scans[i] end
        db.scans = kept
    end
end

function handlers.COMMODITY_SEARCH_RESULTS_UPDATED(itemID)
    local n = Safe(C_AuctionHouse.GetNumCommoditySearchResults, itemID) or 0
    local rows = {}
    for i = 1, math.min(n, MAX_ROWS) do
        local r = Safe(C_AuctionHouse.GetCommoditySearchResultInfo, itemID, i)
        if r and r.unitPrice then
            rows[#rows + 1] = r.unitPrice
            rows[#rows + 1] = r.quantity
        end
    end
    SaveScan("commodity", itemID, 0, rows)
end

function handlers.ITEM_SEARCH_RESULTS_UPDATED(itemKey)
    if type(itemKey) ~= "table" then return end
    local n = Safe(C_AuctionHouse.GetNumItemSearchResults, itemKey) or 0
    local rows = {}
    for i = 1, math.min(n, MAX_ROWS) do
        local r = Safe(C_AuctionHouse.GetItemSearchResultInfo, itemKey, i)
        if r and r.buyoutAmount then
            local qty = r.quantity or 1
            rows[#rows + 1] = math.floor(r.buyoutAmount / math.max(qty, 1))
            rows[#rows + 1] = qty
        end
    end
    SaveScan("item", itemKey.itemID, itemKey.battlePetSpeciesID or 0, rows)
end

local function OnBrowseResults()
    local results = Safe(C_AuctionHouse.GetBrowseResults) or {}
    local rows = {}
    for _, r in ipairs(results) do
        local key = r.itemKey
        if key and r.minPrice then
            rows[#rows + 1] = key.itemID
            rows[#rows + 1] = key.battlePetSpeciesID or 0
            rows[#rows + 1] = r.minPrice
            rows[#rows + 1] = r.totalQuantity or 0
        end
    end
    SaveScan("browse", 0, 0, rows)
end

function handlers.AUCTION_HOUSE_BROWSE_RESULTS_UPDATED()
    Debounce("browse", 0.5, OnBrowseResults)
end
handlers.AUCTION_HOUSE_BROWSE_RESULTS_ADDED = handlers.AUCTION_HOUSE_BROWSE_RESULTS_UPDATED

---------------------------------------------------------------------------
-- Recipes
---------------------------------------------------------------------------

-- What the crafting UI predicts for a recipe with basic (lowest quality) reagents and no
-- concentration: skill vs difficulty, expected quality, and bonus stats in percent.
local function OperationInfo(recipeID)
    local op = Safe(C_TradeSkillUI.GetCraftingOperationInfo, recipeID, {}, nil, false)
    if type(op) ~= "table" then return nil end
    local stats = {}
    for _, b in ipairs(op.bonusStats or {}) do
        if b.bonusStatName then stats[b.bonusStatName] = b.ratingPct or b.bonusStatValue end
    end
    return {
        sk = op.baseSkill, bsk = op.bonusSkill, df = op.baseDifficulty, bdf = op.bonusDifficulty,
        ql = op.craftingQuality, qid = op.craftingQualityID, gq = op.guaranteedCraftingQualityID,
        lo = op.lowerSkillThreshold, hi = op.upperSkillThreshold, qc = op.isQualityCraft,
        cc = op.concentrationCost, ing = op.ingenuityRefund, stats = stats,
    }
end

-- The open profession's concentration: { q = current, max = cap, t = when }.
local function Concentration()
    local ui = C_TradeSkillUI
    local skillLine = Safe(ui.GetProfessionChildSkillLineID)
    local currency = skillLine and Safe(ui.GetConcentrationCurrencyID, skillLine)
    local info = currency and currency ~= 0 and C_CurrencyInfo and Safe(C_CurrencyInfo.GetCurrencyInfo, currency)
    if not info then return nil end
    return { q = info.quantity, max = info.maxQuantity, t = time() }
end

-- A recipe's cooldown: cd = base seconds (the recipe is a spell), cdd = resets daily,
-- cdr = when a running one ends. A ready daily cooldown may report neither; verify in game.
local function Cooldown(recipeID, out)
    local base = Safe((C_Spell and C_Spell.GetSpellBaseCooldown) or GetSpellBaseCooldown, recipeID)
    if type(base) == "number" and base > 0 then out.cd = math.floor(base / 1000) end
    local left, isDay = Safe(C_TradeSkillUI.GetRecipeCooldown, recipeID)
    if isDay then out.cdd = true end
    if type(left) == "number" and left > 0 then out.cdr = time() + math.floor(left) end
end

local function ScanRecipes()
    local ui = C_TradeSkillUI
    if not (ui and ns.char) then return end
    if Safe(ui.IsTradeSkillLinked) or Safe(ui.IsTradeSkillGuild) or Safe(ui.IsNPCCrafting) then
        return -- someone else's (or a guild member's) profession window
    end
    local prof = Safe(ui.GetBaseProfessionInfo)
    local profName = prof and prof.professionName
    if not profName or profName == "" then return end

    local recipes = {}
    local count, ops = 0, 0
    for _, recipeID in ipairs(Safe(ui.GetAllRecipeIDs) or {}) do
        local info = Safe(ui.GetRecipeInfo, recipeID)
        local schematic = info and info.learned and Safe(ui.GetRecipeSchematic, recipeID, false)
        if schematic and schematic.outputItemID then
            local reagents = {}
            for _, slot in ipairs(schematic.reagentSlotSchematics or {}) do
                local basic = not Enum.CraftingReagentType or slot.reagentType == Enum.CraftingReagentType.Basic
                local first = slot.reagents and slot.reagents[1]
                if basic and first and first.itemID and (slot.required ~= false) then
                    reagents[#reagents + 1] = first.itemID
                    reagents[#reagents + 1] = slot.quantityRequired or 1
                end
            end
            recipes[recipeID] = {
                n = info.name,
                o = schematic.outputItemID,
                q = ((schematic.quantityMin or 1) + (schematic.quantityMax or schematic.quantityMin or 1)) / 2,
                r = reagents,
            }
            Cooldown(recipeID, recipes[recipeID])
            NoteItem(schematic.outputItemID)
            for i = 1, #reagents, 2 do NoteItem(reagents[i]) end
            if ops < MAX_OPS then
                recipes[recipeID].op = OperationInfo(recipeID)
                if recipes[recipeID].op then ops = ops + 1 end
            end
            count = count + 1
        end
    end
    if count == 0 then return end
    ns.char.professions = ns.char.professions or {}
    ns.char.professions[profName] = { t = time(), recipes = recipes, conc = Concentration() }
end

function handlers.TRADE_SKILL_LIST_UPDATE()
    Debounce("recipes", 1, ScanRecipes)
end

---------------------------------------------------------------------------
-- Craft results
---------------------------------------------------------------------------

local lastRecipe -- recipe passed to the last CraftRecipe call (result data may not carry it)
local lastCraft  -- what one craft of that call uses up: { rg = {itemID, qty, ...}, rge, ord }

local function ReagentItem(x)
    return type(x) == "table" and (x.itemID or (type(x.reagent) == "table" and x.reagent.itemID)) or nil
end

-- The reagents one craft uses: the ones the UI passed (chosen qualities, optional and finishing
-- reagents), plus any required basic slot it left out, at its lowest quality (flagged estimated).
-- On a crafting order the customer supplies what wasn't passed, so nothing is filled in.
local function CraftReagents(recipeID, passed, orderID)
    local out, used, slots, estimated = {}, {}, {}, nil
    for _, x in ipairs(type(passed) == "table" and passed or {}) do
        local item = ReagentItem(x)
        if item and (x.quantity or 0) > 0 then
            out[#out + 1] = item
            out[#out + 1] = x.quantity
            used[item] = true
            if x.dataSlotIndex then slots[x.dataSlotIndex] = true end
        end
    end
    local order = orderID and orderID ~= 0 and orderID or nil
    local schematic = not order and Safe(C_TradeSkillUI.GetRecipeSchematic, recipeID, false)
    for _, slot in ipairs(schematic and schematic.reagentSlotSchematics or {}) do
        local basic = not Enum.CraftingReagentType or slot.reagentType == Enum.CraftingReagentType.Basic
        local covered = slot.dataSlotIndex ~= nil and slots[slot.dataSlotIndex]
        for _, r in ipairs(slot.reagents or {}) do
            local item = ReagentItem(r)
            if item and used[item] then covered = true end
        end
        local first = ReagentItem(slot.reagents and slot.reagents[1])
        if basic and slot.required ~= false and first and not covered then
            out[#out + 1] = first
            out[#out + 1] = slot.quantityRequired or 1
            estimated = true
        end
    end
    return { rg = out, rge = estimated, ord = order }
end

local function InstallCraftHook()
    local ui = C_TradeSkillUI
    if lastRecipe ~= nil or not (ui and ui.CraftRecipe and hooksecurefunc) then return end
    lastRecipe = false
    hooksecurefunc(ui, "CraftRecipe", function(recipeID, _, reagents, _, orderID)
        lastRecipe = recipeID
        lastCraft = recipeID and CraftReagents(recipeID, reagents, orderID) or nil
    end)
    if ui.CraftEnchant then
        hooksecurefunc(ui, "CraftEnchant", function(recipeID, _, reagents)
            lastRecipe = recipeID
            lastCraft = recipeID and CraftReagents(recipeID, reagents) or nil
        end)
    end
end

function handlers.TRADE_SKILL_ITEM_CRAFTED_RESULT(r)
    local db = ns.db
    if type(r) ~= "table" or not (db and ns.charKey) then return end
    local returned = {}
    for _, x in ipairs(r.resourcesReturned or {}) do
        if x.itemID then
            returned[#returned + 1] = x.itemID
            returned[#returned + 1] = x.quantity or 1
        end
    end
    local recipe = r.recipeID or lastRecipe or nil
    local used = lastCraft and recipe == lastRecipe and lastCraft or nil
    db.crafts = db.crafts or {}
    db.craftSeq = (db.craftSeq or 0) + 1
    db.crafts[#db.crafts + 1] = {
        id = ns.charKey .. ":c" .. db.craftSeq, c = ns.charKey, t = time(),
        r = recipe, item = r.itemID, q = r.quantity or 1,
        ql = r.craftingQuality, mc = r.multicraft or 0, rs = returned,
        cs = r.concentrationSpent or 0, ing = r.hasIngenuityProc or nil, op = r.operationID,
        rg = used and { unpack(used.rg) }, rge = used and used.rge, ord = used and used.ord,
    }
    if #db.crafts > MAX_CRAFTS + 500 then
        local kept = {}
        for i = #db.crafts - MAX_CRAFTS + 1, #db.crafts do kept[#kept + 1] = db.crafts[i] end
        db.crafts = kept
    end
end

function handlers.TRADE_SKILL_SHOW()
    InstallCraftHook()
    if ns.HookOrders then ns.HookOrders() end -- the crafting orders tab lives in this window
    handlers.TRADE_SKILL_LIST_UPDATE()
end

---------------------------------------------------------------------------
-- Owned auctions
---------------------------------------------------------------------------

-- Replaces this character's list each time the AH reports it. buyout is per unit for
-- commodities (cm = 1) and per auction otherwise.
function handlers.OWNER_AUCTIONS_UPDATED()
    local ah = C_AuctionHouse
    if not (ah and ns.char) then return end
    local list = {}
    for i = 1, math.min(Safe(ah.GetNumOwnedAuctions) or 0, MAX_OWNED) do
        local a = Safe(ah.GetOwnedAuctionInfo, i)
        local key = a and a.itemKey
        if key and a.auctionID then
            local keyInfo = Safe(ah.GetItemKeyInfo, key)
            list[#list + 1] = {
                id = a.auctionID, item = key.itemID, v = key.battlePetSpeciesID or 0, il = key.itemLevel,
                q = a.quantity or 1, b = a.buyoutAmount, bid = a.bidAmount, tl = a.timeLeftSeconds,
                st = a.status, cm = keyInfo and (keyInfo.isCommodity and 1 or 0) or nil,
            }
            NoteItem(key.itemID)
        end
    end
    ns.char.auctions = { t = time(), list = list }
end

-- The one AH query Goldmine sends itself: your own auction list (approved; off with
-- GoldmineDB.settings.queryOwnedAuctions = false). Debounced, so a burst of posts asks once.
local function QueryOwned(delay)
    local ah = C_AuctionHouse
    local settings = ns.db and ns.db.settings
    if not (ah and ah.QueryOwnedAuctions) or (settings and settings.queryOwnedAuctions == false) then return end
    Debounce("owned", delay, function() Safe(ah.QueryOwnedAuctions, {}) end)
end

local postHooked = false
function handlers.AUCTION_HOUSE_SHOW()
    local ah = C_AuctionHouse
    if not postHooked and ah and hooksecurefunc then
        postHooked = true
        for _, name in ipairs({ "PostItem", "PostCommodity", "CancelAuction" }) do
            if ah[name] then hooksecurefunc(ah, name, function() QueryOwned(2) end) end
        end
    end
    QueryOwned(0.5)
end

function handlers.AUCTION_HOUSE_AUCTION_CREATED() QueryOwned(2) end
handlers.AUCTION_CANCELED = handlers.AUCTION_HOUSE_AUCTION_CREATED

---------------------------------------------------------------------------
-- Inventory
---------------------------------------------------------------------------

local bankOpen = false

-- Classify Enum.BagIndex entries by name: bags (incl. reagent bag), bank, warband bank.
local function BagGroups()
    local groups = { bags = {}, bank = {}, warband = {} }
    if Enum and Enum.BagIndex then
        for name, index in pairs(Enum.BagIndex) do
            if name:find("Account") then
                groups.warband[#groups.warband + 1] = index
            elseif name:find("Bank") then
                groups.bank[#groups.bank + 1] = index
            elseif name == "Backpack" or name:find("^Bag_%d") or name == "ReagentBag" then
                groups.bags[#groups.bags + 1] = index
            end
        end
    else
        for i = 0, (NUM_BAG_SLOTS or 4) do groups.bags[#groups.bags + 1] = i end
        groups.bank[1] = -1
        for i = (NUM_BAG_SLOTS or 4) + 1, (NUM_BAG_SLOTS or 4) + (NUM_BANKBAGSLOTS or 7) do
            groups.bank[#groups.bank + 1] = i
        end
    end
    return groups
end

local function CountItems(indices)
    local counts, any = {}, false
    local cont = C_Container
    for _, bag in ipairs(indices) do
        local slots = (cont and Safe(cont.GetContainerNumSlots, bag)) or 0
        for slot = 1, slots do
            local info = cont and Safe(cont.GetContainerItemInfo, bag, slot)
            if info and info.itemID then
                if not counts[info.itemID] then NoteItem(info.itemID) end
                counts[info.itemID] = (counts[info.itemID] or 0) + (info.stackCount or 1)
                any = true
            end
        end
    end
    return counts, any
end

-- Gold in the warband bank, readable while a bank is open.
local function ScanWarbandMoney()
    local bankType = Enum and Enum.BankType and Enum.BankType.Account
    local money = bankType and C_Bank and Safe(C_Bank.FetchDepositedMoney, bankType)
    if money and ns.db then ns.db.warbandMoney = { m = money, t = time(), by = ns.charKey } end
end

local function ScanInventory(includeBank)
    if not ns.char then return end
    local groups = BagGroups()
    local inv = ns.char.inventory or {}
    ns.char.inventory = inv
    inv.bags, inv.bagsAt = CountItems(groups.bags), time()
    if includeBank then
        local bank, anyBank = CountItems(groups.bank)
        if anyBank then inv.bank, inv.bankAt = bank, time() end
        local warband, anyWarband = CountItems(groups.warband)
        if anyWarband then ns.db.warband = { items = warband, t = time(), by = ns.charKey } end
        ScanWarbandMoney()
    end
end

function handlers.BAG_UPDATE_DELAYED()
    Debounce("bags", 2, function() ScanInventory(bankOpen) end)
end

function handlers.PLAYER_INTERACTION_MANAGER_FRAME_SHOW(kind)
    local t = Enum and Enum.PlayerInteractionType
    if t and (kind == t.Banker or kind == t.AccountBanker or kind == t.CharacterBanker) then
        bankOpen = true
        Debounce("bags", 0.5, function() ScanInventory(true) end)
    end
end

function handlers.PLAYER_INTERACTION_MANAGER_FRAME_HIDE(kind)
    local t = Enum and Enum.PlayerInteractionType
    if t and (kind == t.Banker or kind == t.AccountBanker or kind == t.CharacterBanker) then
        ScanInventory(true)
        bankOpen = false
    end
end

function handlers.BANKFRAME_OPENED()
    bankOpen = true
    Debounce("bags", 0.5, function() ScanInventory(true) end)
end

function handlers.BANKFRAME_CLOSED()
    bankOpen = false
end

function handlers.ACCOUNT_MONEY()
    if bankOpen then ScanWarbandMoney() end
end

---------------------------------------------------------------------------
-- Professions (every login): which ones each character has, and a nudge to catalogue them
---------------------------------------------------------------------------

local reminded = false

local function ScanProfessions()
    local char = ns.char
    if not (char and GetProfessions and GetProfessionInfo) then return end
    local p1, p2, arch, fish, cook = GetProfessions()
    local profs, stale = {}, {}
    for slot, index in pairs({ p1, p2, arch, fish, cook }) do
        local name, _, rank, maxRank, _, _, skillLine = GetProfessionInfo(index)
        if name then
            profs[name] = { rank = rank, max = maxRank, line = skillLine, t = time() }
            local known = char.professions and char.professions[name]
            -- archaeology (3) and fishing (4) have no recipes
            if slot ~= 3 and slot ~= 4 and (not known or time() - (known.t or 0) > STALE_RECIPES) then
                stale[#stale + 1] = name
            end
        end
    end
    char.profInfo = profs
    if #stale > 0 and not reminded then
        reminded = true
        table.sort(stale)
        local msg = "|cffe6b422Goldmine|r: open your " .. table.concat(stale, ", ")
            .. " window" .. (#stale > 1 and "s" or "") .. " once so the trader sees your recipes."
        if DEFAULT_CHAT_FRAME then DEFAULT_CHAT_FRAME:AddMessage(msg) end
    end
end

function handlers.PLAYER_ENTERING_WORLD()
    Debounce("bags", 3, function() ScanInventory(false) end)
    Debounce("profs", 5, ScanProfessions)
end
handlers.SKILL_LINES_CHANGED = function() Debounce("profs", 2, ScanProfessions) end

---------------------------------------------------------------------------
-- Auction house mail
---------------------------------------------------------------------------

-- Subject patterns from the client's own format strings (English fallbacks).
local function SubjectPattern(fmt, fallback)
    fmt = type(fmt) == "string" and fmt or fallback
    return "^" .. fmt:gsub("([%%%(%)%.%+%-%*%?%[%]%^%$])", "%%%1"):gsub("%%%%s", "(.+)") .. "$"
end

local mailKinds -- built on first use, after the client's globals exist
local function MailKind(subject)
    if not mailKinds then
        mailKinds = {
            { "sold", SubjectPattern(AUCTION_SOLD_MAIL_SUBJECT, "Auction successful: %s") },
            { "won", SubjectPattern(AUCTION_WON_MAIL_SUBJECT, "Auction won: %s") },
            { "outbid", SubjectPattern(AUCTION_OUTBID_MAIL_SUBJECT, "Outbid on %s") },
            { "expired", SubjectPattern(AUCTION_EXPIRED_MAIL_SUBJECT, "Auction expired: %s") },
            { "cancelled", SubjectPattern(AUCTION_REMOVED_MAIL_SUBJECT, "Auction cancelled: %s") },
        }
    end
    for _, k in ipairs(mailKinds) do
        local item = type(subject) == "string" and subject:match(k[2])
        if item then return k[1], item end
    end
end

-- Mail has no id, so a mail is recognized by its contents plus its arrival time (estimated
-- from daysLeft; auction mail lasts 30 days). Identical mails are told apart by count.
local function ScanMail()
    local db = ns.db
    if not (db and ns.charKey and GetInboxNumItems) then return end
    db.mail = db.mail or {}
    db.mailSeq = db.mailSeq or 0
    local now = time()
    local byKey = {}
    for _, m in ipairs(db.mail) do
        if m.c == ns.charKey and now - m.arr < 31 * 86400 then
            byKey[m.k] = byKey[m.k] or {}
            table.insert(byKey[m.k], m)
        end
    end
    local used = {}
    for i = 1, GetInboxNumItems() do
        local _, _, sender, subject, money, cod, daysLeft, hasItem = GetInboxHeaderInfo(i)
        local kind, subjectItem = MailKind(subject)
        local ok, invoiceType, itemName, player, bid, buyout, deposit, consignment, _, _, _, count
        if GetInboxInvoiceInfo then
            ok, invoiceType, itemName, player, bid, buyout, deposit, consignment, _, _, _, count
                = pcall(GetInboxInvoiceInfo, i)
            if not ok then invoiceType = nil end
        end
        if kind or invoiceType then
            local itemID, itemCount
            if hasItem then
                local got, _, id, _, n = pcall(GetInboxItem, i, 1)
                if got then itemID, itemCount = id, n end
            end
            local key = table.concat({ kind or "", invoiceType or "", itemName or subjectItem or "", player or "",
                tostring(bid or 0), tostring(buyout or 0), tostring(deposit or 0), tostring(consignment or 0),
                tostring(money or 0), tostring(itemID or 0), tostring(count or itemCount or 0) }, "|")
            local arr = now - math.floor((30 - (daysLeft or 30)) * 86400)
            local match
            for _, m in ipairs(byKey[key] or {}) do
                if not used[m] and math.abs(m.arr - arr) <= MAIL_MATCH then match = m; break end
            end
            if match then
                used[match] = true
                match.seen = now
            else
                db.mailSeq = db.mailSeq + 1
                local m = {
                    id = ns.charKey .. ":m" .. db.mailSeq, c = ns.charKey, k = key, arr = arr, seen = now,
                    kind = kind, inv = invoiceType, item = itemName or subjectItem, iid = itemID,
                    n = count or itemCount, who = player, bid = bid, buy = buyout, dep = deposit,
                    cut = consignment, money = money, cod = cod, from = sender,
                }
                db.mail[#db.mail + 1] = m
                used[m] = true
            end
        end
    end
    if #db.mail > MAX_MAIL + 500 then
        local kept = {}
        for i = #db.mail - MAX_MAIL + 1, #db.mail do kept[#kept + 1] = db.mail[i] end
        db.mail = kept
    end
end

function handlers.MAIL_INBOX_UPDATE()
    Debounce("mail", 1, ScanMail)
end

---------------------------------------------------------------------------
-- Crafting orders
---------------------------------------------------------------------------

local function ItemIDFromLink(link)
    return type(link) == "string" and tonumber(link:match("|Hitem:(%d+)")) or nil
end

-- Reads the orders the crafting orders window last requested (no requests of our own).
local function ScanOrders()
    local co = C_CraftingOrders
    local db = ns.db
    if not (co and db and ns.charKey) then return end
    db.craftOrders = db.craftOrders or {}
    local now = time()
    local n = 0
    for _, o in ipairs(Safe(co.GetCrafterOrders) or {}) do
        if o.orderID then
            local reagents = {}
            for _, r in ipairs(o.reagents or {}) do
                local item = r.reagent or r.reagentInfo or {}
                if item.itemID then
                    reagents[#reagents + 1] = item.itemID
                    reagents[#reagents + 1] = item.quantity or 1
                end
            end
            local rewards = {}
            for _, r in ipairs(o.npcOrderRewards or {}) do
                local id = ItemIDFromLink(r.itemLink)
                if id then
                    rewards[#rewards + 1] = id
                    rewards[#rewards + 1] = r.count or 1
                end
            end
            local prev = db.craftOrders[o.orderID]
            db.craftOrders[o.orderID] = {
                c = ns.charKey, first = prev and prev.first or now, seen = now, type = o.orderType,
                state = o.orderState, item = o.itemID, r = o.spellID, minq = o.minQuality, tip = o.tipAmount,
                cut = o.consortiumCut, exp = o.expirationTime, claim = o.claimEndTime,
                recraft = o.isRecraft or nil, ok = o.isFulfillable, rstate = o.reagentState,
                who = o.customerName, npc = o.npcCustomerCreatureID, rg = reagents, rw = rewards,
            }
            n = n + 1
        end
    end
    local buckets = {}
    for _, b in ipairs(Safe(co.GetCrafterBuckets) or {}) do
        buckets[#buckets + 1] = { item = b.itemID, r = b.skillLineAbilityID or b.spellID, n = b.numAvailable,
                                  avg = b.tipAmountAvg, max = b.tipAmountMax }
    end
    if #buckets > 0 then
        db.craftBuckets = db.craftBuckets or {}
        db.craftBuckets[ns.charKey] = { t = now, list = buckets }
    end
    -- Keep the newest MAX_ORDERS.
    local count, oldest, oldestID = 0, nil, nil
    for _ in pairs(db.craftOrders) do count = count + 1 end
    while count > MAX_ORDERS do
        oldest, oldestID = nil, nil
        for id, o in pairs(db.craftOrders) do
            if not oldest or o.seen < oldest then oldest, oldestID = o.seen, id end
        end
        db.craftOrders[oldestID] = nil
        count = count - 1
    end
    return n
end

local ordersHooked = false
local function HookOrders()
    local co = C_CraftingOrders
    if ordersHooked or not (co and hooksecurefunc) then return end
    ordersHooked = true
    for _, name in ipairs({ "RequestCrafterOrders" }) do
        if co[name] then
            hooksecurefunc(co, name, function()
                Debounce("orders", 1.5, ScanOrders)
                C_Timer.After(4, ScanOrders) -- slow responses
            end)
        end
    end
end

function handlers.CRAFTINGORDERS_SHOW_CRAFTER()
    HookOrders()
    Debounce("orders", 1.5, ScanOrders)
end
handlers.CRAFTINGORDERS_UPDATE_ORDER_COUNT = function() Debounce("orders", 1.5, ScanOrders) end
handlers.CRAFTINGORDERS_HIDE_CRAFTER = function() ScanOrders() end

---------------------------------------------------------------------------

frame:SetScript("OnEvent", function(_, event, ...)
    handlers[event](...)
end)

for event in pairs(handlers) do
    pcall(frame.RegisterEvent, frame, event)
end

ns.SaveScan = SaveScan -- for tests
ns.ScanOrders = ScanOrders
ns.HookOrders = HookOrders
HookOrders()
