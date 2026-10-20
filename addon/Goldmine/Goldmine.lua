-- Goldmine: logs every change to the player's money with a best-guess source.
--
-- How attribution works:
--   * PLAYER_MONEY tells us *that* money changed; we diff GetMoney() to get the amount.
--   * "Hints" are created by hooking the API calls that move money (TakeInboxMoney,
--     RepairAllItems, TakeTaxiNode, ...). A hint may carry an exact expected amount
--     and/or a sign, which lets us match it to the right money change.
--   * "Contexts" are the NPC windows that are open (vendor, mailbox, auction house,
--     trainer, ...). If no hint matches, the open (or just-closed) context labels it.
--   * Classification is deferred by RESOLVE_DELAY so hints that arrive a moment after
--     the money event (e.g. QUEST_TURNED_IN) still get matched.
--   * Anything we can't explain is logged as "unknown" — nothing is ever dropped.

local ADDON_NAME, ns = ...

local DB_VERSION = 1
local RESOLVE_DELAY = 0.3 -- seconds to wait for late hints before classifying
local HINT_TTL = 3        -- seconds a hint can be matched before/after its creation
local CLOSE_GRACE = 2     -- seconds a closed window still counts as the context

local db, char, charKey, sessionId
local lastMoney
local debugMode = false

local hints = {}    -- { src, amount?, sign?, low?, meta?, at }
local contexts = {} -- name -> { opened, closed?, meta? }
local pending = {}  -- money changes awaiting classification

local session = { start = 0, net = 0, gained = 0, spent = 0, count = 0, bySource = {} }

---------------------------------------------------------------------------
-- Helpers
---------------------------------------------------------------------------

local function Print(msg)
    DEFAULT_CHAT_FRAME:AddMessage("|cffffd100Goldmine|r: " .. msg)
end

local function FormatMoney(copper, signed)
    local sign = ""
    if copper < 0 then
        sign = "-"
        copper = -copper
    elseif signed then
        sign = "+"
    end
    local g = math.floor(copper / 10000)
    local s = math.floor(copper / 100) % 100
    local c = copper % 100
    local parts = {}
    if g > 0 then parts[#parts + 1] = g .. "|cffffd700g|r" end
    if s > 0 then parts[#parts + 1] = s .. "|cffc7c7cfs|r" end
    if c > 0 or #parts == 0 then parts[#parts + 1] = c .. "|cffeda55fc|r" end
    return sign .. table.concat(parts, " ")
end

ns.Print, ns.FormatMoney = Print, FormatMoney
ns.commands = {} -- extra slash subcommands registered by other files: name -> fn(arg)

local function Hook(owner, name, fn)
    if type(owner) == "string" then
        owner, name, fn = _G, owner, name
    end
    if owner and type(owner[name]) == "function" then
        if owner == _G then
            hooksecurefunc(name, fn)
        else
            hooksecurefunc(owner, name, fn)
        end
    end
end

local function Safe(fn, ...)
    if type(fn) ~= "function" then return nil end
    local ok, a, b, c, d, e, f, g, h = pcall(fn, ...)
    if ok then return a, b, c, d, e, f, g, h end
    return nil
end

local function ToSnake(name)
    return (name:gsub("(%l)(%u)", "%1_%2"):lower())
end

---------------------------------------------------------------------------
-- Hints and contexts
---------------------------------------------------------------------------

local function AddHint(src, opts)
    opts = opts or {}
    hints[#hints + 1] = {
        src = src,
        amount = opts.amount,
        sign = opts.sign,
        low = opts.low,
        meta = opts.meta,
        at = GetTime(),
    }
end

local function OpenContext(name, meta)
    contexts[name] = { opened = GetTime(), meta = meta }
end

local function CloseContext(name)
    local ctx = contexts[name]
    if ctx and not ctx.closed then
        ctx.closed = GetTime()
    end
end

-- The most recently opened context that is open, or closed within CLOSE_GRACE.
local function CurrentContext(now)
    local bestName, best
    for name, ctx in pairs(contexts) do
        local live = not ctx.closed or (now - ctx.closed) <= CLOSE_GRACE
        if live and (not best or ctx.opened > best.opened) then
            bestName, best = name, ctx
        end
    end
    if best then
        return { name = bestName, meta = best.meta }
    end
end

local function HintMatches(h, p, pass)
    if math.abs(h.at - p.at) > HINT_TTL then return false end
    if pass == 1 then return h.amount ~= nil and h.amount == p.delta end
    if h.amount ~= nil then return false end
    if h.sign and (p.delta > 0) ~= (h.sign > 0) then return false end
    if pass == 2 then return not h.low end
    return h.low
end

-- pass 1: exact amount; pass 2: sign/unconstrained; pass 3: low-priority hints.
local function TakeHint(p, pass)
    for i, h in ipairs(hints) do
        if HintMatches(h, p, pass) then
            table.remove(hints, i)
            return h
        end
    end
end

local function PruneHints(now)
    for i = #hints, 1, -1 do
        if now - hints[i].at > HINT_TTL + RESOLVE_DELAY then
            table.remove(hints, i)
        end
    end
    for name, ctx in pairs(contexts) do
        if ctx.closed and now - ctx.closed > CLOSE_GRACE + RESOLVE_DELAY then
            contexts[name] = nil
        end
    end
end

---------------------------------------------------------------------------
-- Recording
---------------------------------------------------------------------------

-- Where the player is right now; stored with every entry.
local function Location()
    return {
        ts = time(),
        zone = GetRealZoneText(),
        sub = GetSubZoneText(),
        map = C_Map and Safe(C_Map.GetBestMapForUnit, "player"),
    }
end

local function Record(delta, balance, src, meta, info)
    info = info or Location()
    char.seq = char.seq + 1
    char.money = balance
    char.lastSeen = time()

    if meta and next(meta) == nil then meta = nil end
    local entry = {
        id = charKey .. ":" .. char.seq,
        c = charKey,
        t = info.ts,
        d = delta,
        b = balance,
        src = src,
        zone = info.zone,
        sub = info.sub,
        map = info.map,
        s = sessionId,
        m = meta,
    }
    db.log[#db.log + 1] = entry

    if delta ~= 0 then
        session.net = session.net + delta
        session.count = session.count + 1
        if delta > 0 then
            session.gained = session.gained + delta
        else
            session.spent = session.spent - delta
        end
        session.bySource[src] = (session.bySource[src] or 0) + delta
    end

    if debugMode then
        Print(("%s  %s"):format(FormatMoney(delta, true), src))
    end
    return entry
end

local function Classify(p)
    local h = TakeHint(p, 1) or TakeHint(p, 2)
    if h then return h.src, h.meta end
    if p.ctx then return p.ctx.name, p.ctx.meta end
    h = TakeHint(p, 3)
    if h then return h.src, h.meta end
    return "unknown", nil
end

local function ProcessPending(force)
    local now = GetTime()
    while pending[1] and (force or now - pending[1].at >= RESOLVE_DELAY) do
        local p = table.remove(pending, 1)
        local src, meta = Classify(p)
        local copy
        if meta then
            copy = {}
            for k, v in pairs(meta) do copy[k] = v end
        end
        Record(p.delta, p.balance, src, copy, p)
    end
    PruneHints(now)
    if pending[1] then
        C_Timer.After(RESOLVE_DELAY, function() ProcessPending(false) end)
    end
end

local function OnMoneyChanged()
    local money = GetMoney()
    local delta = money - lastMoney
    if delta == 0 then return end
    lastMoney = money

    local now = GetTime()
    local p = Location()
    p.delta, p.balance, p.at, p.ctx = delta, money, now, CurrentContext(now)
    pending[#pending + 1] = p
    C_Timer.After(RESOLVE_DELAY, function() ProcessPending(false) end)
end

---------------------------------------------------------------------------
-- Context sources: NPC interaction windows
---------------------------------------------------------------------------

-- Labels for Enum.PlayerInteractionType names; unlisted types become snake_case.
local INTERACTION_LABELS = {
    Merchant = "vendor",
    MailInfo = "mail",
    Auctioneer = "auction",
    TaxiNode = "flight",
    Trainer = "trainer",
    TradePartner = "trade",
    GuildBanker = "guild_bank",
    Banker = "bank",
    AccountBanker = "warband_bank",
    Transmogrifier = "transmog",
    Barber = "barber",
    BlackMarketAuctioneer = "black_market",
    ItemUpgrade = "item_upgrade",
    VoidStorageBanker = "void_storage",
    QuestGiver = "quest",
    Gossip = "npc",
}

local interactionNames = {}

local function NpcMeta()
    local npc = UnitName("npc")
    return npc and { npc = npc } or nil
end

local function OnInteractionShow(kind)
    local name = interactionNames[kind]
    if not name then return end
    local label = INTERACTION_LABELS[name] or ToSnake(name)
    if label == "trade" then return end -- handled by TRADE_SHOW (has partner name)
    OpenContext(label, NpcMeta())
end

local function OnInteractionHide(kind)
    local name = interactionNames[kind]
    if not name then return end
    CloseContext(INTERACTION_LABELS[name] or ToSnake(name))
end

-- Fallback for clients without the interaction manager.
local LEGACY_CONTEXT_EVENTS = {
    MERCHANT_SHOW = { "vendor", true }, MERCHANT_CLOSED = { "vendor", false },
    MAIL_SHOW = { "mail", true }, MAIL_CLOSED = { "mail", false },
    AUCTION_HOUSE_SHOW = { "auction", true }, AUCTION_HOUSE_CLOSED = { "auction", false },
    TAXIMAP_OPENED = { "flight", true }, TAXIMAP_CLOSED = { "flight", false },
    TRAINER_SHOW = { "trainer", true }, TRAINER_CLOSED = { "trainer", false },
    GUILDBANKFRAME_OPENED = { "guild_bank", true }, GUILDBANKFRAME_CLOSED = { "guild_bank", false },
    BANKFRAME_OPENED = { "bank", true }, BANKFRAME_CLOSED = { "bank", false },
    GOSSIP_SHOW = { "npc", true }, GOSSIP_CLOSED = { "npc", false },
}

---------------------------------------------------------------------------
-- Hint sources: hooks on money-moving API calls
---------------------------------------------------------------------------

local function InstallHooks()
    -- Mail: taking money (incl. auction house sale invoices) and COD payments.
    local function OnTakeMailMoney(index)
        local _, _, sender, subject, money, cod = Safe(GetInboxHeaderInfo, index)
        if money and money > 0 then
            local meta = { from = sender, subject = subject }
            local src = "mail"
            local invoiceType, itemName, player, bid, _, deposit, cut, _, _, _, count = Safe(GetInboxInvoiceInfo, index)
            if invoiceType == "seller" then
                src = "auction_sale"
                meta.item, meta.buyer, meta.price, meta.deposit, meta.cut = itemName, player, bid, deposit, cut
                meta.qty = count -- units sold, for FIFO cost basis on the server
            end
            AddHint(src, { amount = money, meta = meta })
        end
        if cod and cod > 0 then
            AddHint("mail_cod", { amount = -cod, meta = { from = sender, subject = subject } })
        end
    end
    Hook("TakeInboxMoney", OnTakeMailMoney)
    Hook("AutoLootMailItem", OnTakeMailMoney)
    Hook("TakeInboxItem", function(index)
        local _, _, sender, subject, _, cod = Safe(GetInboxHeaderInfo, index)
        if cod and cod > 0 then
            AddHint("mail_cod", { amount = -cod, meta = { from = sender, subject = subject } })
        end
    end)

    -- Mail: sending gold and postage.
    Hook("SendMail", function(recipient, subject)
        local sent = Safe(GetSendMailMoney) or 0
        local postage = Safe(GetSendMailPrice) or 0
        local meta = { to = recipient, subject = subject, sent = sent, postage = postage }
        AddHint(sent > 0 and "mail_send" or "postage", { amount = -(sent + postage), meta = meta })
    end)

    -- Repairs (guild-funded repairs don't touch personal money).
    Hook("RepairAllItems", function(useGuild)
        if useGuild then return end
        local cost = Safe(GetRepairAllCost)
        if cost and cost > 0 then
            AddHint("repair", { amount = -cost })
        else
            AddHint("repair", { sign = -1 })
        end
    end)

    -- Flight paths.
    Hook("TakeTaxiNode", function(slot)
        local cost = Safe(TaxiNodeCost, slot)
        local meta = { dest = Safe(TaxiNodeName, slot) }
        if cost and cost > 0 then
            AddHint("flight", { amount = -cost, meta = meta })
        end
    end)

    -- Vendor purchases and sales (item names for the log).
    Hook("BuyMerchantItem", function(index, quantity)
        AddHint("vendor", { sign = -1, meta = { item = Safe(GetMerchantItemLink, index), qty = quantity, npc = UnitName("npc") } })
    end)
    Hook("BuybackItem", function(index)
        AddHint("vendor_buyback", { sign = -1, meta = { item = Safe(GetBuybackItemLink, index) } })
    end)
    local function OnUseContainerItem(bag, slot)
        local vendor = contexts.vendor
        if vendor and not vendor.closed then
            local link = C_Container and Safe(C_Container.GetContainerItemLink, bag, slot)
                or Safe(GetContainerItemLink, bag, slot)
            AddHint("vendor", { sign = 1, meta = { item = link, npc = vendor.meta and vendor.meta.npc } })
        end
    end
    if C_Container then Hook(C_Container, "UseContainerItem", OnUseContainerItem) end
    Hook("UseContainerItem", OnUseContainerItem)

    -- Auction house (modern API).
    if C_AuctionHouse then
        Hook(C_AuctionHouse, "PostItem", function() AddHint("auction_deposit", { sign = -1 }) end)
        Hook(C_AuctionHouse, "PostCommodity", function() AddHint("auction_deposit", { sign = -1 }) end)
        Hook(C_AuctionHouse, "PlaceBid", function() AddHint("auction_buy", { sign = -1 }) end)
        Hook(C_AuctionHouse, "ConfirmCommoditiesPurchase", function(itemID, quantity)
            AddHint("auction_buy", { sign = -1, meta = { item_id = itemID, qty = quantity } })
        end)
        Hook(C_AuctionHouse, "CancelAuction", function() AddHint("auction_cancel", { sign = -1 }) end)
    end

    -- Guild bank and warband bank deposits/withdrawals.
    Hook("DepositGuildBankMoney", function(amount)
        AddHint("guild_bank", { amount = -amount })
    end)
    Hook("WithdrawGuildBankMoney", function(amount)
        AddHint("guild_bank", { amount = amount })
    end)
    if C_Bank then
        Hook(C_Bank, "DepositMoney", function(_, amount)
            AddHint("warband_bank", { amount = -amount })
        end)
        Hook(C_Bank, "WithdrawMoney", function(_, amount)
            AddHint("warband_bank", { amount = amount })
        end)
    end
end

---------------------------------------------------------------------------
-- Init
---------------------------------------------------------------------------

local function InitDB()
    GoldmineDB = GoldmineDB or {}
    db = GoldmineDB
    ns.db = db
    db.version = db.version or DB_VERSION
    db.log = db.log or {}
    db.chars = db.chars or {}
    db.settings = db.settings or {}
    debugMode = db.settings.debug and true or false
end

local function InitCharacter()
    local name = UnitName("player")
    local realm = GetNormalizedRealmName and GetNormalizedRealmName() or GetRealmName()
    charKey = name .. "-" .. realm
    ns.charKey = charKey
    char = db.chars[charKey]
    if not char then
        char = { seq = 0 }
        db.chars[charKey] = char
    end
    ns.char = char
    char.name, char.realm = name, realm
    char.class = select(2, UnitClass("player"))
    char.faction = UnitFactionGroup("player")
    -- Forever's AH is shared per region + realm type, so keep the region for market lookups.
    char.region = Safe(GetCurrentRegionName)

    sessionId = charKey .. ":" .. time()
    session.start = GetTime()
end

-- Runs on the first PLAYER_ENTERING_WORLD of a UI session. At PLAYER_LOGIN the zone isn't
-- known yet (and money may not be either), so the baseline is taken here instead.
local function StartTracking()
    lastMoney = GetMoney()

    if char.money == nil then
        Record(0, lastMoney, "baseline")
    elseif char.money ~= lastMoney then
        -- Money moved while the addon wasn't watching (disabled, crash, lost SavedVariables).
        Record(lastMoney - char.money, lastMoney, "reconcile")
    end
end

---------------------------------------------------------------------------
-- Events
---------------------------------------------------------------------------

local frame = CreateFrame("Frame")
local handlers = {}

function handlers.ADDON_LOADED(name)
    if name == ADDON_NAME then
        InitDB()
        frame:UnregisterEvent("ADDON_LOADED")
    end
end

local hooksInstalled = false

function handlers.PLAYER_LOGIN()
    InitCharacter()
    if not hooksInstalled then
        InstallHooks()
        hooksInstalled = true
    end
    frame:RegisterEvent("PLAYER_ENTERING_WORLD")
end

function handlers.PLAYER_ENTERING_WORLD()
    frame:UnregisterEvent("PLAYER_ENTERING_WORLD")
    StartTracking()
    frame:RegisterEvent("PLAYER_MONEY")
end

function handlers.PLAYER_LOGOUT()
    ProcessPending(true)
end

function handlers.PLAYER_MONEY()
    OnMoneyChanged()
end

function handlers.PLAYER_INTERACTION_MANAGER_FRAME_SHOW(kind)
    OnInteractionShow(kind)
end

function handlers.PLAYER_INTERACTION_MANAGER_FRAME_HIDE(kind)
    OnInteractionHide(kind)
end

function handlers.TRADE_SHOW()
    OpenContext("trade", { partner = UnitName("NPC") })
end

function handlers.TRADE_CLOSED()
    CloseContext("trade")
end

function handlers.LOOT_OPENED()
    OpenContext("loot")
end

function handlers.LOOT_CLOSED()
    CloseContext("loot")
end

function handlers.CHAT_MSG_MONEY()
    -- Coin loot (including party shares). Low priority: only used if nothing better fits.
    AddHint("loot", { sign = 1, low = true })
end

function handlers.QUEST_TURNED_IN(questID, _, money)
    if money and money > 0 then
        local title = C_QuestLog and Safe(C_QuestLog.GetTitleForQuestID, questID)
        AddHint("quest", { amount = money, meta = { quest_id = questID, quest = title } })
    end
end

frame:SetScript("OnEvent", function(_, event, ...)
    handlers[event](...)
end)

local function TryRegister(event)
    return pcall(frame.RegisterEvent, frame, event)
end

for _, event in ipairs({
    "ADDON_LOADED", "PLAYER_LOGIN", "PLAYER_LOGOUT",
    "TRADE_SHOW", "TRADE_CLOSED", "LOOT_OPENED", "LOOT_CLOSED",
    "CHAT_MSG_MONEY", "QUEST_TURNED_IN",
}) do
    TryRegister(event)
end

if Enum and Enum.PlayerInteractionType
    and TryRegister("PLAYER_INTERACTION_MANAGER_FRAME_SHOW")
    and TryRegister("PLAYER_INTERACTION_MANAGER_FRAME_HIDE") then
    for name, value in pairs(Enum.PlayerInteractionType) do
        interactionNames[value] = name
    end
else
    for event, spec in pairs(LEGACY_CONTEXT_EVENTS) do
        if TryRegister(event) then
            local label, open = spec[1], spec[2]
            handlers[event] = function()
                if open then OpenContext(label, NpcMeta()) else CloseContext(label) end
            end
        end
    end
end

---------------------------------------------------------------------------
-- Slash commands
---------------------------------------------------------------------------

local function ShowSummary()
    local elapsed = math.max(GetTime() - session.start, 1)
    local perHour = math.floor(session.net / elapsed * 3600)
    Print(("Session: %s net (%s/hr) over %d min, %d changes"):format(
        FormatMoney(session.net, true), FormatMoney(perHour, true), math.floor(elapsed / 60), session.count))
    Print(("  in %s, out %s"):format(FormatMoney(session.gained), FormatMoney(-session.spent)))
    local sources = {}
    for src, amount in pairs(session.bySource) do sources[#sources + 1] = { src, amount } end
    table.sort(sources, function(a, b) return math.abs(a[2]) > math.abs(b[2]) end)
    for _, row in ipairs(sources) do
        Print(("  %-14s %s"):format(row[1], FormatMoney(row[2], true)))
    end
end

local function ShowRecent(n)
    n = n or 10
    local shown = 0
    for i = #db.log, 1, -1 do
        local e = db.log[i]
        if e.c == charKey then
            Print(("%s  %s  %s  %s"):format(date("%m-%d %H:%M", e.t), FormatMoney(e.d, true), e.src, e.zone or ""))
            shown = shown + 1
            if shown >= n then break end
        end
    end
    if shown == 0 then Print("No entries yet.") end
end

local function Prune(days)
    if not days or days < 1 then
        Print("Usage: /goldmine prune <days>  (run the desktop sync first!)")
        return
    end
    local cutoff = time() - days * 86400
    local kept, removed = {}, 0
    for _, e in ipairs(db.log) do
        if e.t >= cutoff then kept[#kept + 1] = e else removed = removed + 1 end
    end
    db.log = kept
    Print(("Removed %d entries older than %d days. %d remain."):format(removed, days, #kept))
end

SLASH_GOLDMINE1 = "/goldmine"
SLASH_GOLDMINE2 = "/gmine"
SlashCmdList.GOLDMINE = function(msg)
    local cmd, arg = (msg or ""):lower():match("^%s*(%S*)%s*(.-)%s*$")
    if cmd == "" or cmd == "session" then
        ShowSummary()
    elseif cmd == "recent" then
        ShowRecent(tonumber(arg))
    elseif cmd == "debug" then
        debugMode = not debugMode
        db.settings.debug = debugMode
        Print("Debug " .. (debugMode and "on: every change will be printed." or "off."))
    elseif cmd == "prune" then
        Prune(tonumber(arg))
    elseif ns.commands[cmd] then
        ns.commands[cmd](arg)
    elseif cmd == "stats" then
        local oldest = db.log[1] and date("%Y-%m-%d", db.log[1].t) or "n/a"
        Print(("%d entries stored (oldest %s). Balance: %s"):format(#db.log, oldest, FormatMoney(GetMoney())))
    else
        Print("/goldmine [session] | recent [n] | stats | debug | prune <days> | crafts | deals | tasks")
    end
end

-- Exposed for the offline test harness.
Goldmine_Internal = {
    hints = hints,
    contexts = contexts,
    pending = pending,
    session = session,
    ProcessPending = ProcessPending,
}
