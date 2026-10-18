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
for _, file in ipairs({ "Data.lua", "Goldmine.lua", "Market.lua", "Advice.lua" }) do
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
}
fire("TRADE_SKILL_SHOW")
fire("TRADE_SKILL_LIST_UPDATE")
advance(2)
local tailoring = GoldmineDB.chars["Snow-Testrealm"].professions.Tailoring
expect("learned recipe recorded", tailoring.recipes[100].o == 2996 and tailoring.recipes[100].r[1] == 2589
    and tailoring.recipes[100].r[2] == 2 and #tailoring.recipes[100].r == 2)
expect("unlearned recipe skipped", tailoring.recipes[101] == nil)

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
fire("BAG_UPDATE_DELAYED")
advance(3)
local inv = GoldmineDB.chars["Snow-Testrealm"].inventory
expect("bags counted (incl. reagent bag)", inv.bags[2589] == 25 and inv.bags[2592] == 3 and inv.bank == nil)
fire("PLAYER_INTERACTION_MANAGER_FRAME_SHOW", 8)
advance(1)
expect("bank and warband counted", inv.bank[2996] == 7 and GoldmineDB.warband.items[4306] == 50)
fire("PLAYER_INTERACTION_MANAGER_FRAME_HIDE", 8)

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
