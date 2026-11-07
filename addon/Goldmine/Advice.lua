-- Goldmine advice: shows what `goldmine.py push` computed on the desktop (Data.lua).
-- Data only refreshes on login or /reload, since addons can't read files at any other time.

local _, ns = ...

-- Data.lua holds one price set per realm you play (kind "realm") and one per game version's
-- region-wide commodity market (kind "commodities"). We use the set whose realm list contains
-- the current realm, plus the commodity set of the same game. That keeps e.g. retail and
-- Mists Classic prices apart even though they share item ids.

local EMPTY = { prices = {}, crafts = {}, deals = {}, realms = {} }
local realmSet, commoditySet, chosen

local function Norm(name)
    return ((name or ""):lower():gsub("[^%w]", ""))
end

-- The game this client is, so a realm or commodity set from another game is never used.
-- Forever (interface 1.60.x = 16xxx) is checked by build first: its WOW_PROJECT_ID may well
-- read as mainline, and retail prices must never show there.
local function ClientGame()
    local build = GetBuildInfo and select(4, GetBuildInfo())
    if build and build >= 16000 and build < 20000 then return "forever" end
    local id = WOW_PROJECT_ID
    if not id then return nil end
    if id == WOW_PROJECT_MAINLINE then return "retail" end
    if WOW_PROJECT_CLASSIC and id == WOW_PROJECT_CLASSIC then return "classic1x" end
    if WOW_PROJECT_BURNING_CRUSADE_CLASSIC and id == WOW_PROJECT_BURNING_CRUSADE_CLASSIC then return "classicann" end
    if WOW_PROJECT_MISTS_CLASSIC and id == WOW_PROJECT_MISTS_CLASSIC then return "classic" end
    return "unknown"
end

local function ChooseSets()
    if chosen then return end
    chosen = true
    local sets = (Goldmine_Data and Goldmine_Data.sets) or {}
    local realm = Norm(GetRealmName and GetRealmName())
    local client = ClientGame()
    -- Forever houses carry a faction: one realm name can play on a Horde and an Alliance house.
    local faction = ((UnitFactionGroup and UnitFactionGroup("player")) or ""):lower()
    for _, set in ipairs(sets) do
        if set.kind == "realm" and (client == nil or set.game == client)
                and (not set.faction or set.faction == faction) then
            for _, r in ipairs(set.realms or {}) do
                if Norm(r) == realm then realmSet = set end
            end
        end
    end
    -- No game known (no realm match, unrecognised client): show nothing rather than guess.
    local game = (realmSet and realmSet.game) or client
    if not game then return end
    for _, set in ipairs(sets) do
        if set.kind == "commodities" and set.game == game then
            commoditySet = set
            break
        end
    end
end

local function Price(itemID)
    ChooseSets()
    return (realmSet and realmSet.prices[itemID]) or (commoditySet and commoditySet.prices[itemID])
end

local function Describe()
    ChooseSets()
    if not (realmSet or commoditySet) then return "no market data for this realm" end
    local set = realmSet or commoditySet
    local where = realmSet and table.concat(realmSet.realms, "/") or "commodities only"
    return set.game .. " - " .. where
end

local function ItemName(itemID)
    local name = C_Item and C_Item.GetItemNameByID and C_Item.GetItemNameByID(itemID)
    return name or (GetItemInfo and GetItemInfo(itemID)) or ("item " .. itemID)
end

local function Age()
    local generated = (Goldmine_Data and Goldmine_Data.generated) or 0
    if generated == 0 then return "never (run goldmine.py push)" end
    local mins = math.floor((time() - generated) / 60)
    if mins < 120 then return mins .. " min ago" end
    return math.floor(mins / 60) .. " h ago"
end

---------------------------------------------------------------------------
-- Tooltips
---------------------------------------------------------------------------

local function AddPriceLines(tooltip, itemID)
    local p = Price(itemID)
    if not p then return end
    local mv, min, sold = p[1], p[2], p[3]
    local money = ns.FormatMoney
    if mv then tooltip:AddDoubleLine("|cffffd100Goldmine|r market", money(mv), 1, 1, 1, 1, 1, 1) end
    if min and min ~= mv then tooltip:AddDoubleLine("  lowest listed", money(min), 0.8, 0.8, 0.8, 1, 1, 1) end
    if sold and sold > 0 then
        tooltip:AddDoubleLine("  sold / day (est.)", tostring(sold), 0.8, 0.8, 0.8, 1, 1, 1)
    end
end

if TooltipDataProcessor and Enum and Enum.TooltipDataType then
    TooltipDataProcessor.AddTooltipPostCall(Enum.TooltipDataType.Item, function(tooltip, data)
        -- pcall: under Midnight's addon restrictions some tooltip data can be off-limits.
        pcall(function()
            if data and data.id then AddPriceLines(tooltip, data.id) end
        end)
    end)
end

---------------------------------------------------------------------------
-- Slash commands
---------------------------------------------------------------------------

ns.commands.crafts = function(arg)
    ChooseSets()
    local crafts, money = (realmSet or EMPTY).crafts, ns.FormatMoney
    local limit = tonumber(arg) or 10
    ns.Print(("Most profitable crafts, %s (data %s):"):format(Describe(), Age()))
    if #crafts == 0 then ns.Print("  none yet") end
    for i = 1, math.min(limit, #crafts) do
        local _, name, outputID, cost, revenue, sold, crafters = unpack(crafts[i])
        ns.Print(("  %s: %s profit (cost %s, sells %s, ~%s/day) - %s"):format(
            name or ItemName(outputID), money(revenue - cost, true), money(cost), money(revenue), sold, crafters))
    end
end

ns.commands.deals = function(arg)
    ChooseSets()
    local deals, money = {}, ns.FormatMoney
    for _, set in ipairs({ realmSet or EMPTY, commoditySet or EMPTY }) do
        for _, d in ipairs(set.deals) do deals[#deals + 1] = d end
    end
    table.sort(deals, function(a, b) return (a[3] - a[2]) * math.min(a[4], 20) > (b[3] - b[2]) * math.min(b[4], 20) end)
    local limit = tonumber(arg) or 10
    ns.Print(("Listed below market, %s (data %s):"):format(Describe(), Age()))
    if #deals == 0 then ns.Print("  none right now") end
    for i = 1, math.min(limit, #deals) do
        local itemID, min, mv, sold = unpack(deals[i])
        ns.Print(("  %s at %s, market %s (%d%% off, ~%s/day)"):format(
            ItemName(itemID), money(min), money(mv), math.floor((1 - min / mv) * 100 + 0.5), sold))
    end
end

ns.commands.market = function()
    ns.Print("Using " .. Describe() .. " (data " .. Age() .. ")")
end

ns.AddPriceLines = AddPriceLines -- for tests
ns.ResetSets = function() chosen, realmSet, commoditySet = nil, nil, nil end

---------------------------------------------------------------------------
-- "Blizzard serves this game's AH now": the server probes the API hourly for game versions it
-- can't collect and lists those that started answering in Data.lua (ah_live). Until a market
-- for this realm exists, say so loudly once per login or /reload.
---------------------------------------------------------------------------

local LABELS = { forever = "Forever", classic1x = "Classic Era", classicann = "Anniversary" }

local function LiveNotice()
    ChooseSets()
    if realmSet then return nil end -- already priced: the market was added
    local game = ClientGame()
    local region = ((GetCurrentRegionName and GetCurrentRegionName()) or ""):lower()
    for _, live in ipairs((Goldmine_Data and Goldmine_Data.ah_live) or {}) do
        if live.game == game and (region == "" or live.region == region) then
            return ("Blizzard's API now serves %s's auction house! Add this realm on your Goldmine "
                .. "server's Markets page to collect its prices."):format(LABELS[game] or game)
        end
    end
end

local noticeFrame = CreateFrame("Frame")
noticeFrame:RegisterEvent("PLAYER_ENTERING_WORLD")
noticeFrame:SetScript("OnEvent", function(self)
    self:UnregisterEvent("PLAYER_ENTERING_WORLD")
    C_Timer.After(8, function() -- after the login chat spam
        local msg = LiveNotice()
        if not msg then return end
        ns.Print("|cffff4040" .. msg .. "|r")
        if RaidNotice_AddMessage and RaidWarningFrame then
            pcall(RaidNotice_AddMessage, RaidWarningFrame, msg, ChatTypeInfo and ChatTypeInfo.RAID_WARNING
                or { r = 1, g = 0.3, b = 0.1 })
        end
        if PlaySound and SOUNDKIT and SOUNDKIT.RAID_WARNING then pcall(PlaySound, SOUNDKIT.RAID_WARNING) end
    end)
end)
ns.LiveNotice = LiveNotice -- for tests
