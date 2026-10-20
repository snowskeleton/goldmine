-- Offline test harness: mocks the bits of the WoW API Goldmine uses and simulates play.
-- Run from the addon/ directory:  lua tests/test_goldmine.lua

local clock = 1000
local money = 50000
local timers = {}
local frames = {}
local hooks = {}
local npcName

function GetTime() return clock end
function time() return 1760000000 + math.floor(clock) end
date = os.date
function GetMoney() return money end
function GetRealZoneText() return "Elwynn Forest" end
function GetSubZoneText() return "Goldshire" end
function GetNormalizedRealmName() return "Testrealm" end
function GetCurrentRegionName() return "US" end
function UnitName(unit)
    if unit == "player" then return "Snow" end
    return npcName
end
function UnitClass() return "Mage", "MAGE" end
function UnitFactionGroup() return "Alliance" end
C_Map = { GetBestMapForUnit = function() return 37 end }
C_QuestLog = { GetTitleForQuestID = function(id) return "Quest " .. id end }
C_Timer = { After = function(delay, fn) timers[#timers + 1] = { at = clock + delay, fn = fn } end }
Enum = { PlayerInteractionType = { Merchant = 5, MailInfo = 17, TaxiNode = 7, Auctioneer = 21, Barber = 25 } }

local printed = {}
DEFAULT_CHAT_FRAME = { AddMessage = function(_, msg) printed[#printed + 1] = msg end }
SlashCmdList = {}

function hooksecurefunc(owner, name, fn)
    if type(owner) == "string" then owner, name, fn = _G, owner, name end
    local orig = owner[name]
    owner[name] = function(...)
        local r = { orig(...) }
        fn(...)
        return (table.unpack or unpack)(r)
    end
    hooks[name] = true
end

function CreateFrame()
    local f = { events = {} }
    function f:RegisterEvent(e) self.events[e] = true end
    function f:UnregisterEvent(e) self.events[e] = nil end
    function f:SetScript(_, fn) self.onEvent = fn end
    frames[#frames + 1] = f
    return f
end

-- Money-moving APIs. The server applies money changes *after* the call, so tests
-- call these, then change money and fire PLAYER_MONEY themselves.
local inbox = {
    { sender = "Auction House", subject = "Auction successful: Linen Cloth", money = 9500, invoice = { "seller", "Linen Cloth", "Buyerguy", 10000, 10000, 100, 600 } },
    { sender = "Altchar", subject = "gold", money = 20000, cod = 0 },
}
function GetInboxHeaderInfo(i)
    local m = inbox[i]
    return nil, nil, m.sender, m.subject, m.money, m.cod or 0
end
function GetInboxInvoiceInfo(i)
    local inv = inbox[i].invoice
    if inv then return (table.unpack or unpack)(inv) end
end
function TakeInboxMoney() end
function TakeInboxItem() end
function AutoLootMailItem() end
local sendMoney = 0
function GetSendMailMoney() return sendMoney end
function GetSendMailPrice() return 30 end
function SendMail() end
function GetRepairAllCost() return 1234, true end
function RepairAllItems() end
function TaxiNodeCost() return 550 end
function TaxiNodeName() return "Stormwind" end
function TakeTaxiNode() end
function GetMerchantItemLink(i) return "[Item " .. i .. "]" end
function BuyMerchantItem() end
C_Container = {
    UseContainerItem = function() end,
    GetContainerItemLink = function(bag, slot) return "[Bag " .. bag .. "-" .. slot .. "]" end,
}

local function fire(event, ...)
    for _, f in ipairs(frames) do
        if f.events[event] then f.onEvent(f, event, ...) end
    end
end

local function advance(seconds)
    local target = clock + seconds
    while true do
        table.sort(timers, function(a, b) return a.at < b.at end)
        local t = timers[1]
        if not t or t.at > target then break end
        table.remove(timers, 1)
        clock = t.at
        t.fn()
    end
    clock = target
end

local function changeMoney(delta)
    money = money + delta
    fire("PLAYER_MONEY")
end

local failures = 0
local function expect(label, cond, detail)
    if cond then
        print("ok   " .. label)
    else
        failures = failures + 1
        print("FAIL " .. label .. (detail and ("  -- " .. detail) or ""))
    end
end

local function last(n)
    local log = GoldmineDB.log
    return log[#log - (n or 1) + 1]
end

---------------------------------------------------------------------------

local ns = {}
unpack = unpack or table.unpack -- WoW is Lua 5.1
local tooltipHook
TooltipDataProcessor = { AddTooltipPostCall = function(_, fn) tooltipHook = fn end }
Enum.TooltipDataType = { Item = 0 }
-- Data.lua is generated per machine (and untracked); the Advice tests below supply their own Goldmine_Data.
for _, file in ipairs({ "Goldmine.lua", "Market.lua", "Advice.lua" }) do
    assert(loadfile("Goldmine/" .. file))("Goldmine", ns)
end
fire("ADDON_LOADED", "Goldmine")
fire("PLAYER_LOGIN")
expect("no baseline before entering world", #GoldmineDB.log == 0)
changeMoney(0) -- PLAYER_MONEY isn't registered yet; must be ignored
fire("PLAYER_ENTERING_WORLD", true, false)

expect("baseline recorded on first login", last().src == "baseline" and last().b == 50000 and last().d == 0)
expect("baseline has location", last().zone == "Elwynn Forest" and last().map == 37)
expect("region stored", GoldmineDB.chars["Snow-Testrealm"].region == "US")
fire("PLAYER_ENTERING_WORLD", false, false)
expect("zoning doesn't re-baseline", #GoldmineDB.log == 1)

-- Vendor: open window, sell an item, buy an item, repair.
npcName = "Vendorbot"
fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 5)
C_Container.UseContainerItem(0, 3)
changeMoney(250)
BuyMerchantItem(2, 1)
changeMoney(-100)
RepairAllItems(false)
changeMoney(-1234)
advance(1)
expect("vendor sale", last(3).src == "vendor" and last(3).d == 250 and last(3).m.item == "[Bag 0-3]", last(3).src)
expect("vendor buy", last(2).src == "vendor" and last(2).d == -100 and last(2).m.item == "[Item 2]")
expect("repair matched by amount", last(1).src == "repair" and last(1).d == -1234)
fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", 5)
npcName = nil

-- Mail: take AH sale money, take gold from an alt, send gold with postage.
advance(5)
fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 17)
TakeInboxMoney(1)
TakeInboxMoney(2)
changeMoney(9500)
changeMoney(20000)
sendMoney = 5000
SendMail("Altchar", "here you go", "")
changeMoney(-5030)
sendMoney = 0
advance(1)
expect("auction sale via mail invoice", last(3).src == "auction_sale" and last(3).m.buyer == "Buyerguy" and last(3).m.cut == 600)
expect("mail from alt", last(2).src == "mail" and last(2).m.from == "Altchar" and last(2).d == 20000)
expect("mail send includes postage", last(1).src == "mail_send" and last(1).d == -5030 and last(1).m.postage == 30)
fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", 17)

-- Quest reward where QUEST_TURNED_IN arrives *after* PLAYER_MONEY.
advance(5)
changeMoney(777)
advance(0.1)
fire("QUEST_TURNED_IN", 42, 1000, 777)
advance(1)
expect("late quest hint still matched", last().src == "quest" and last().m.quest == "Quest 42", last().src)

-- Coin loot.
advance(5)
fire("LOOT_OPENED")
fire("CHAT_MSG_MONEY", "You loot 12 Copper")
changeMoney(12)
fire("LOOT_CLOSED")
advance(1)
expect("loot", last().src == "loot" and last().d == 12)

-- Flight path: money changes just after the taxi window closes (grace period).
advance(5)
fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 7)
TakeTaxiNode(3)
fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", 7)
advance(0.5)
changeMoney(-550)
advance(1)
expect("flight with destination", last().src == "flight" and last().m.dest == "Stormwind")

-- Unlisted interaction type falls back to snake_case enum name (Barber -> barber).
advance(5)
fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 25)
changeMoney(-300)
advance(1)
expect("barber context", last().src == "barber")
fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", 25)

-- Nothing open, no hints: unknown.
advance(10)
changeMoney(-1)
advance(1)
expect("unexplained change is unknown", last().src == "unknown")

-- Pending entries are flushed on logout even if the timer hasn't fired.
changeMoney(5)
fire("PLAYER_LOGOUT")
expect("flushed on logout", last().d == 5)

-- Balances are consistent: each balance = previous balance + delta.
local ok = true
for i = 2, #GoldmineDB.log do
    local a, b = GoldmineDB.log[i - 1], GoldmineDB.log[i]
    if a.b + b.d ~= b.b then ok = false end
end
expect("running balance consistent", ok)
expect("ids are unique and sequential", last().id == "Snow-Testrealm:" .. #GoldmineDB.log)

-- Re-login after money changed outside the addon -> reconcile entry.
money = money + 999
fire("PLAYER_LOGIN")
fire("PLAYER_ENTERING_WORLD", false, true)
expect("reconcile on login", last().src == "reconcile" and last().d == 999)

-- Slash commands don't error.
SlashCmdList.GOLDMINE("")
SlashCmdList.GOLDMINE("recent 3")
SlashCmdList.GOLDMINE("stats")
expect("slash commands print output", #printed > 5)

---------------------------------------------------------------------------
-- Market.lua: AH scans, recipes, inventory
---------------------------------------------------------------------------

local commodityResults = { { unitPrice = 15, quantity = 200 }, { unitPrice = 16, quantity = 40 } }
C_AuctionHouse = {
    GetNumCommoditySearchResults = function() return #commodityResults end,
    GetCommoditySearchResultInfo = function(_, i) return commodityResults[i] end,
    GetNumItemSearchResults = function() return 1 end,
    GetItemSearchResultInfo = function() return { buyoutAmount = 25000, quantity = 1 } end,
    GetBrowseResults = function()
        return { { itemKey = { itemID = 2589 }, minPrice = 15, totalQuantity = 240 },
                 { itemKey = { itemID = 82800, battlePetSpeciesID = 42 }, minPrice = 90000, totalQuantity = 2 } }
    end,
}
fire("COMMODITY_SEARCH_RESULTS_UPDATED", 2589)
commodityResults[3] = { unitPrice = 20, quantity = 500 } -- more results stream in
fire("COMMODITY_SEARCH_RESULTS_UPDATED", 2589)
local scans = GoldmineDB.scans
expect("commodity scan merged into one", #scans == 1 and #scans[1].rows == 6 and scans[1].item == 2589)
fire("ITEM_SEARCH_RESULTS_UPDATED", { itemID = 2575 })
expect("item scan", scans[2].kind == "item" and scans[2].rows[1] == 25000)
fire("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")
advance(1)
expect("browse scan", scans[3].kind == "browse" and scans[3].rows[5] == 82800 and scans[3].rows[6] == 42)

Enum.CraftingReagentType = { Basic = 1, Modifying = 2 }
C_TradeSkillUI = {
    IsTradeSkillLinked = function() return false end,
    IsTradeSkillGuild = function() return false end,
    GetBaseProfessionInfo = function() return { professionName = "Tailoring" } end,
    GetAllRecipeIDs = function() return { 100, 101 } end,
    GetRecipeInfo = function(id) return { name = "Bolt of Linen Cloth", learned = id == 100 } end,
    GetRecipeSchematic = function()
        return { outputItemID = 2996, quantityMin = 1, quantityMax = 1, reagentSlotSchematics = {
            { reagentType = 1, quantityRequired = 2, reagents = { { itemID = 2589 } } },
            { reagentType = 2, quantityRequired = 1, reagents = { { itemID = 999 } } }, -- optional, skipped
        } }
    end,
    GetCraftingOperationInfo = function(id, reagents, guid, conc)
        assert(type(reagents) == "table" and conc == false)
        return { recipeID = id, baseSkill = 80, bonusSkill = 5, baseDifficulty = 100, bonusDifficulty = 0,
                 craftingQuality = 2, craftingQualityID = 2, isQualityCraft = true, concentrationCost = 30,
                 lowerSkillThreshold = 75, upperSkillThreshold = 125, ingenuityRefund = 15,
                 bonusStats = { { bonusStatName = "Multicraft", bonusStatValue = 120, ratingPct = 12.5 },
                                { bonusStatName = "Resourcefulness", bonusStatValue = 90, ratingPct = 9 } } }
    end,
    GetProfessionChildSkillLineID = function() return 2915 end,
    GetConcentrationCurrencyID = function() return 3040 end,
    CraftRecipe = function() end,
}
C_CurrencyInfo = { GetCurrencyInfo = function(id) return id == 3040 and { quantity = 640, maxQuantity = 1000 } end }
fire("TRADE_SKILL_SHOW")
fire("TRADE_SKILL_LIST_UPDATE")
advance(2)
local tailoring = GoldmineDB.chars["Snow-Testrealm"].professions.Tailoring
expect("learned recipe recorded", tailoring.recipes[100].o == 2996 and tailoring.recipes[100].r[1] == 2589
    and tailoring.recipes[100].r[2] == 2 and #tailoring.recipes[100].r == 2)
expect("unlearned recipe skipped", tailoring.recipes[101] == nil)
local op = tailoring.recipes[100].op
expect("operation info recorded", op and op.sk == 80 and op.df == 100 and op.ql == 2 and op.cc == 30
    and op.stats.Multicraft == 12.5 and op.stats.Resourcefulness == 9)
expect("concentration recorded", tailoring.conc and tailoring.conc.q == 640 and tailoring.conc.max == 1000)

-- A craft: the recipe comes from the hooked CraftRecipe call when the result doesn't carry it.
C_TradeSkillUI.CraftRecipe(100, 1)
fire("TRADE_SKILL_ITEM_CRAFTED_RESULT", { itemID = 2996, quantity = 2, multicraft = 1, craftingQuality = 3,
    concentrationSpent = 30, resourcesReturned = { { itemID = 2589, quantity = 1 } }, operationID = 77 })
fire("TRADE_SKILL_ITEM_CRAFTED_RESULT", "not a table")
local craft = GoldmineDB.crafts and GoldmineDB.crafts[1]
expect("craft result recorded", craft and #GoldmineDB.crafts == 1 and craft.r == 100 and craft.q == 2
    and craft.mc == 1 and craft.ql == 3 and craft.cs == 30 and craft.rs[1] == 2589 and craft.rs[2] == 1
    and craft.id == "Snow-Testrealm:c1")

Enum.BagIndex = { Backpack = 0, Bag_1 = 1, ReagentBag = 5, CharacterBankTab_1 = 6, AccountBankTab_1 = 12 }
Enum.PlayerInteractionType.Banker = 8
local containers = {
    [0] = { { itemID = 2589, stackCount = 20 }, { itemID = 2589, stackCount = 5 } },
    [5] = { { itemID = 2592, stackCount = 3 } },
    [6] = { { itemID = 2996, stackCount = 7 } },
    [12] = { { itemID = 4306, stackCount = 50 } },
}
C_Container.GetContainerNumSlots = function(bag) return containers[bag] and #containers[bag] or 0 end
C_Container.GetContainerItemInfo = function(bag, slot) return containers[bag] and containers[bag][slot] end
local itemInfoCalls = 0
C_Item = { GetItemInfo = function(id)
    itemInfoCalls = itemInfoCalls + 1
    if id == 2592 then return nil end -- not cached by the client yet
    -- name, link, quality, ilvl, minLevel, type, subType, stackCount, equipLoc, texture, sellPrice,
    -- classID, subclassID, bindType, expansionID
    return "Item " .. id, nil, 1, 1, 0, "Tradeskill", "Cloth", 200, "", 0, 10, 7, 5, 0, 0
end }
Enum.BankType = { Character = 0, Guild = 1, Account = 2 }
C_Bank = C_Bank or {}
local deposited = 1234567
C_Bank.FetchDepositedMoney = function(kind) return kind == 2 and deposited or 0 end
fire("BAG_UPDATE_DELAYED")
advance(3)
local inv = GoldmineDB.chars["Snow-Testrealm"].inventory
expect("bags counted (incl. reagent bag)", inv.bags[2589] == 25 and inv.bags[2592] == 3 and inv.bank == nil)
fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 8)
advance(1)
expect("bank and warband counted", inv.bank[2996] == 7 and GoldmineDB.warband.items[4306] == 50)
expect("warband gold read while the bank is open", GoldmineDB.warbandMoney.m == 1234567
    and GoldmineDB.warbandMoney.by == "Snow-Testrealm")
deposited = 2000000
fire("ACCOUNT_MONEY")
expect("warband gold follows deposits", GoldmineDB.warbandMoney.m == 2000000)
fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", 8)
local ii = GoldmineDB.itemInfo
expect("item info noted once per item", ii[2589].s == 200 and ii[2589].v == 10 and ii[2589].c == 7
    and ii[2592] == nil and ii[4306] ~= nil)
local calls = itemInfoCalls
fire("BAG_UPDATE_DELAYED")
advance(3)
expect("known items aren't looked up again", itemInfoCalls - calls == 1, itemInfoCalls - calls) -- just 2592

-- Owned auctions: recorded from the event the Auctions tab triggers; no query unless opted in.
local queried = 0
C_AuctionHouse.QueryOwnedAuctions = function() queried = queried + 1 end
C_AuctionHouse.GetNumOwnedAuctions = function() return 2 end
C_AuctionHouse.GetOwnedAuctionInfo = function(i)
    return ({ { auctionID = 501, itemKey = { itemID = 2589, itemLevel = 5 }, quantity = 40, buyoutAmount = 18,
                timeLeftSeconds = 86000, status = 0 },
              { auctionID = 502, itemKey = { itemID = 2996, itemLevel = 10 }, quantity = 1, buyoutAmount = 5000,
                bidAmount = 4000, timeLeftSeconds = 3600, status = 1 } })[i]
end
C_AuctionHouse.GetItemKeyInfo = function(key) return { isCommodity = key.itemID == 2589 } end
C_AuctionHouse.PostCommodity = function() end
C_AuctionHouse.CancelAuction = function() end
fire("AUCTION_HOUSE_SHOW")
advance(1)
expect("owned auctions requested when the AH opens", queried == 1, queried)
C_AuctionHouse.PostCommodity(2589, 40, 18)
C_AuctionHouse.PostCommodity(2589, 40, 18)
fire("AUCTION_HOUSE_AUCTION_CREATED", 77)
advance(3)
expect("one debounced request after a burst of posts", queried == 2, queried)
C_AuctionHouse.CancelAuction(501)
advance(3)
expect("requested again after a cancel", queried == 3, queried)
GoldmineDB.settings.queryOwnedAuctions = false
fire("AUCTION_HOUSE_SHOW")
C_AuctionHouse.PostCommodity(2589, 1, 18)
advance(3)
expect("no requests when turned off", queried == 3, queried)
GoldmineDB.settings.queryOwnedAuctions = nil
fire("OWNER_AUCTIONS_UPDATED")
local owned = GoldmineDB.chars["Snow-Testrealm"].auctions
expect("owned auctions recorded", #owned.list == 2 and owned.list[1].id == 501 and owned.list[1].cm == 1
    and owned.list[1].b == 18 and owned.list[2].cm == 0 and owned.list[2].st == 1 and owned.list[2].tl == 3600)

-- Professions on login, and one reminder per session for uncatalogued ones.
local profs = { [1] = { "Tailoring", 90, 100, 197 }, [2] = { "Alchemy", 30, 100, 171 }, [4] = { "Fishing", 5, 100, 356 } }
function GetProfessions() return 1, 2, nil, 4, nil end
function GetProfessionInfo(i)
    local p = profs[i]
    return p[1], 0, p[2], p[3], 0, 0, p[4]
end
local before = #printed
fire("PLAYER_ENTERING_WORLD", false, false)
advance(6)
local pinfo = GoldmineDB.chars["Snow-Testrealm"].profInfo
expect("professions recorded on login", pinfo.Tailoring.rank == 90 and pinfo.Alchemy.max == 100
    and pinfo.Fishing.line == 356)
expect("reminder names only the uncatalogued crafting profession", #printed == before + 1
    and printed[#printed]:find("Alchemy") ~= nil and printed[#printed]:find("Tailoring") == nil
    and printed[#printed]:find("Fishing") == nil, printed[#printed])
fire("PLAYER_ENTERING_WORLD", false, false)
advance(6)
expect("reminder only once per session", #printed == before + 1)

-- Auction mail: invoices and sold/expired/won mail, recognized again on the next inbox update.
local mailbox = {
    { "Auction House", "Auction successful: Linen Cloth", 1710, 0, 29.5, false,
      { "seller", "Linen Cloth", "Buyerguy", 1800, 1800, 30, 90, 0, 0, 0, 100 } },
    { "Auction House", "Auction successful: Linen Cloth", 1710, 0, 29.5, false,
      { "seller", "Linen Cloth", "Buyerguy", 1800, 1800, 30, 90, 0, 0, 0, 100 } }, -- an identical second sale
    { "Auction House", "Auction expired: Bolt of Linen Cloth", 0, 0, 29.9, true, nil, { "Bolt of Linen Cloth", 2996, 0, 5 } },
    { "Auction House", "Auction won: Wool Cloth", 0, 0, 29, true,
      { "buyer", "Wool Cloth", "Sellerguy", 0, 400, 0, 0, 0, 0, 0, 20 }, { "Wool Cloth", 2592, 0, 20 } },
    { "Altchar", "gold", 50000, 0, 3, false },
}
function GetInboxNumItems() return #mailbox end
GetInboxHeaderInfo = function(i)
    local m = mailbox[i]
    return nil, nil, m[1], m[2], m[3], m[4], m[5], m[6]
end
GetInboxInvoiceInfo = function(i) if mailbox[i][7] then return unpack(mailbox[i][7]) end end
function GetInboxItem(i) if mailbox[i][8] then return unpack(mailbox[i][8]) end end
fire("MAIL_INBOX_UPDATE")
advance(2)
local mail = GoldmineDB.mail
expect("auction mail recorded, other mail skipped", #mail == 4, #mail)
expect("sale invoice fields", mail[1].kind == "sold" and mail[1].inv == "seller" and mail[1].who == "Buyerguy"
    and mail[1].buy == 1800 and mail[1].dep == 30 and mail[1].cut == 90 and mail[1].n == 100 and mail[1].money == 1710)
expect("expired mail carries the returned item", mail[3].kind == "expired" and mail[3].iid == 2996 and mail[3].n == 5
    and mail[3].item == "Bolt of Linen Cloth")
expect("won mail", mail[4].kind == "won" and mail[4].inv == "buyer" and mail[4].iid == 2592)
advance(600)
for _, m in ipairs(mailbox) do m[5] = m[5] - 600 / 86400 end -- ten minutes later
table.remove(mailbox, 3) -- the expired item was taken
fire("MAIL_INBOX_UPDATE")
advance(2)
expect("same mails aren't recorded twice", #mail == 4, #mail)
table.insert(mailbox, 1, { "Auction House", "Auction successful: Linen Cloth", 1710, 0, 30, false,
    { "seller", "Linen Cloth", "Buyerguy", 1800, 1800, 30, 90, 0, 0, 0, 100 } })
fire("MAIL_INBOX_UPDATE")
advance(2)
expect("a new identical sale is a new mail", #mail == 5 and mail[5].id == "Snow-Testrealm:m5")

-- Crafting orders: read after the crafting orders UI requests them.
local orders = {
    { orderID = 9001, itemID = 2996, spellID = 100, orderType = 0, orderState = 0, expirationTime = time() + 3600,
      minQuality = 2, tipAmount = 50000, consortiumCut = 2500, isFulfillable = true, customerName = "Buyer",
      reagents = { { reagentSlot = 1, reagent = { itemID = 2589, quantity = 2 } } } },
    { orderID = 9002, itemID = 2996, spellID = 100, orderType = 3, npcCustomerCreatureID = 12345, tipAmount = 10000,
      npcOrderRewards = { { itemLink = "|cff|Hitem:210814::::|h[Artisan's Acuity]|h|r", count = 3 } } },
}
C_CraftingOrders = {
    RequestCrafterOrders = function() end,
    GetCrafterOrders = function() return orders end,
    GetCrafterBuckets = function() return { { itemID = 2996, spellID = 100, numAvailable = 4, tipAmountAvg = 30000,
                                              tipAmountMax = 60000 } } end,
}
fire("TRADE_SKILL_SHOW")
advance(2)
C_CraftingOrders.RequestCrafterOrders({})
advance(5)
local co = GoldmineDB.craftOrders
expect("crafting orders recorded", co and co[9001] and co[9001].tip == 50000 and co[9001].minq == 2
    and co[9001].rg[1] == 2589 and co[9001].rg[2] == 2 and co[9001].who == "Buyer")
expect("patron order rewards", co[9002].type == 3 and co[9002].rw[1] == 210814 and co[9002].rw[2] == 3)
expect("order buckets", GoldmineDB.craftBuckets["Snow-Testrealm"].list[1].n == 4)

---------------------------------------------------------------------------
-- Advice.lua: tooltips and commands from pushed Data.lua
---------------------------------------------------------------------------

Goldmine_Data = {
    generated = time() - 600,
    sets = {
        { kind = "realm", game = "classic", region = "us", name = "us-mists-pagle", realms = { "Pagle", "Test Realm" },
          prices = { [2996] = { 1, 1, 1 } }, crafts = {}, deals = {} },
        { kind = "commodities", game = "classic", region = "us", name = "mists-c", realms = {},
          prices = { [2589] = { 2, 2, 2 } }, crafts = {}, deals = {} },
        { kind = "realm", game = "retail", region = "us", name = "us-testrealm", realms = { "Testrealm" },
          prices = { [2996] = { 100, 40, 240 } },
          crafts = { { 100, "Bolt of Linen Cloth", 2996, 30, 38, 240, "Snow-Testrealm" } },
          deals = { { 2996, 40, 100, 240 } } },
        { kind = "commodities", game = "retail", region = "us", name = "us-commodities", realms = {},
          prices = { [2589] = { 900, 600, 50 } }, crafts = {}, deals = {} },
    },
}
function GetRealmName() return "Testrealm" end
ns.ResetSets()
local lines = {}
local tooltip = { AddDoubleLine = function(_, l, r) lines[#lines + 1] = l .. "=" .. r end }
tooltipHook(tooltip, { id = 2996 })
tooltipHook(tooltip, { id = 12345 }) -- no data: nothing added
expect("tooltip shows market, lowest, sold", #lines == 3 and lines[1]:find("market") ~= nil)
expect("realm set chosen by realm name (not the Mists one)", lines[2]:find("40") ~= nil, lines[2])
lines = {}
tooltipHook(tooltip, { id = 2589 })
expect("commodity price from the same game", #lines == 3 and lines[1]:find("9") ~= nil, lines[1])

-- A realm with no configured market falls back to the client's game commodity set.
GetRealmName = function() return "Somewhere Else" end
WOW_PROJECT_ID, WOW_PROJECT_MAINLINE, WOW_PROJECT_MISTS_CLASSIC = 19, 1, 19
ns.ResetSets()
lines = {}
tooltipHook(tooltip, { id = 2589 })
expect("fallback picks Mists commodities in a Mists client", #lines == 2 and lines[1]:find("2") ~= nil, lines[1])
GetRealmName = function() return "Testrealm" end
WOW_PROJECT_ID = nil
ns.ResetSets()
local before = #printed
SlashCmdList.GOLDMINE("crafts")
SlashCmdList.GOLDMINE("deals")
expect("crafts/deals print", #printed - before == 4 and printed[before + 2]:find("Bolt of Linen Cloth") ~= nil
    and printed[before + 1]:find("retail %- Testrealm") ~= nil,
    printed[before + 2])
expect("deal shows 60% off", printed[#printed]:find("60%% off") ~= nil, printed[#printed])

-- Write a sample SavedVariables file for the desktop tool's tests.
local out = arg[1]
if out then
    local function ser(v, indent)
        if type(v) == "string" then return ("%q"):format(v) end
        if type(v) ~= "table" then return tostring(v) end
        local lines = { "{" }
        local n = #v
        for i = 1, n do lines[#lines + 1] = indent .. "\t" .. ser(v[i], indent .. "\t") .. ", -- [" .. i .. "]" end
        for k, val in pairs(v) do
            if not (type(k) == "number" and k >= 1 and k <= n) then
                local key = type(k) == "string" and ("[%q]"):format(k) or ("[" .. k .. "]")
                lines[#lines + 1] = indent .. "\t" .. key .. " = " .. ser(val, indent .. "\t") .. ","
            end
        end
        lines[#lines + 1] = indent .. "}"
        return table.concat(lines, "\n")
    end
    local f = assert(io.open(out, "w"))
    f:write("\nGoldmineDB = " .. ser(GoldmineDB, "") .. "\n")
    f:close()
    print("wrote " .. out)
end

print(failures == 0 and "\nall passed" or ("\n" .. failures .. " failed"))
os.exit(failures == 0 and 0 or 1)
