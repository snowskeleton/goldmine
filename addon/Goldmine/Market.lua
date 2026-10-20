-- Goldmine market data: passively records what the game already shows you.
--
--   * AH prices from searches you run (commodity, item, and browse results). No queries are
--     sent on your behalf; this only reads results the AH UI requested.
--   * Your learned recipes and their reagents (when a profession window is open).
--   * Bag, bank, and warband bank contents, and the gold in the warband bank.
--   * Your own auctions, when the AH's Auctions tab lists them (OWNER_AUCTIONS_UPDATED). Set
--     GoldmineDB.settings.queryOwnedAuctions = true to also ask for that list when the AH opens.
--   * Crafting operation info per recipe (skill, difficulty, expected quality, multicraft/
--     resourcefulness, concentration cost) and the result of every craft you make.
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

local frame = CreateFrame("Frame")
local handlers = {}
local pendingTimers = {}

local function Safe(fn, ...)
    if type(fn) ~= "function" then return nil end
    local ok, a, b, c = pcall(fn, ...)
    if ok then return a, b, c end
end

-- Remember an item's stack size, binding, expansion and vendor price (what the item API
-- doesn't say), once per item. Skipped while the client hasn't cached the item yet.
local function NoteItem(itemID)
    local db = ns.db
    if not (db and itemID) then return end
    db.itemInfo = db.itemInfo or {}
    if db.itemInfo[itemID] then return end
    db.itemInfoCount = db.itemInfoCount or 0
    if db.itemInfoCount >= MAX_ITEM_INFO then return end
    local get = (C_Item and C_Item.GetItemInfo) or GetItemInfo
    local ok, name, _, _, _, _, _, _, stack, _, _, sell, class, subclass, bind, expansion = pcall(get, itemID)
    if not (ok and name) then return end
    db.itemInfo[itemID] = { s = stack, v = sell, c = class, sc = subclass, b = bind, x = expansion, t = time() }
    db.itemInfoCount = db.itemInfoCount + 1
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

local function InstallCraftHook()
    local ui = C_TradeSkillUI
    if lastRecipe ~= nil or not (ui and ui.CraftRecipe and hooksecurefunc) then return end
    lastRecipe = false
    hooksecurefunc(ui, "CraftRecipe", function(recipeID) lastRecipe = recipeID end)
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
    db.crafts = db.crafts or {}
    db.craftSeq = (db.craftSeq or 0) + 1
    db.crafts[#db.crafts + 1] = {
        id = ns.charKey .. ":c" .. db.craftSeq, c = ns.charKey, t = time(),
        r = r.recipeID or lastRecipe or nil, item = r.itemID, q = r.quantity or 1,
        ql = r.craftingQuality, mc = r.multicraft or 0, rs = returned,
        cs = r.concentrationSpent or 0, ing = r.hasIngenuityProc or nil, op = r.operationID,
    }
    if #db.crafts > MAX_CRAFTS + 500 then
        local kept = {}
        for i = #db.crafts - MAX_CRAFTS + 1, #db.crafts do kept[#kept + 1] = db.crafts[i] end
        db.crafts = kept
    end
end

function handlers.TRADE_SKILL_SHOW()
    InstallCraftHook()
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

function handlers.AUCTION_HOUSE_SHOW()
    local ah = C_AuctionHouse
    if ns.db and ns.db.settings and ns.db.settings.queryOwnedAuctions and ah and ah.QueryOwnedAuctions then
        Safe(ah.QueryOwnedAuctions, {}) -- opt-in only: the one query Goldmine sends itself
    end
end

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

function handlers.PLAYER_ENTERING_WORLD()
    Debounce("bags", 3, function() ScanInventory(false) end)
end

---------------------------------------------------------------------------

frame:SetScript("OnEvent", function(_, event, ...)
    handlers[event](...)
end)

for event in pairs(handlers) do
    pcall(frame.RegisterEvent, frame, event)
end

ns.SaveScan = SaveScan -- for tests
