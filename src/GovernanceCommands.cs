using System;
using System.Text;
using ColossalFramework;

namespace SkylinesAgentBridge
{
    public static class GovernanceCommands
    {
        private static readonly BudgetTarget[] BudgetTargets = new BudgetTarget[]
        {
            new BudgetTarget(ItemClass.Service.Road, ItemClass.SubService.None),
            new BudgetTarget(ItemClass.Service.Electricity, ItemClass.SubService.None),
            new BudgetTarget(ItemClass.Service.Water, ItemClass.SubService.None),
            new BudgetTarget(ItemClass.Service.Garbage, ItemClass.SubService.None),
            new BudgetTarget(ItemClass.Service.HealthCare, ItemClass.SubService.None),
            new BudgetTarget(ItemClass.Service.FireDepartment, ItemClass.SubService.None),
            new BudgetTarget(ItemClass.Service.PoliceDepartment, ItemClass.SubService.None),
            new BudgetTarget(ItemClass.Service.Education, ItemClass.SubService.None),
            new BudgetTarget(ItemClass.Service.Beautification, ItemClass.SubService.None),
            new BudgetTarget(ItemClass.Service.PublicTransport, ItemClass.SubService.PublicTransportBus),
            new BudgetTarget(ItemClass.Service.PublicTransport, ItemClass.SubService.PublicTransportMetro),
            new BudgetTarget(ItemClass.Service.PublicTransport, ItemClass.SubService.PublicTransportTrain),
            new BudgetTarget(ItemClass.Service.PublicTransport, ItemClass.SubService.PublicTransportShip),
            new BudgetTarget(ItemClass.Service.PublicTransport, ItemClass.SubService.PublicTransportPlane),
            new BudgetTarget(ItemClass.Service.PublicTransport, ItemClass.SubService.PublicTransportTaxi)
        };

        // Zoned tax income has no service budget, so it is read separately.
        private static readonly BudgetTarget[] TaxTargets = new BudgetTarget[]
        {
            new BudgetTarget(ItemClass.Service.Residential, ItemClass.SubService.ResidentialLow),
            new BudgetTarget(ItemClass.Service.Residential, ItemClass.SubService.ResidentialHigh),
            new BudgetTarget(ItemClass.Service.Commercial, ItemClass.SubService.CommercialLow),
            new BudgetTarget(ItemClass.Service.Commercial, ItemClass.SubService.CommercialHigh),
            new BudgetTarget(ItemClass.Service.Industrial, ItemClass.SubService.IndustrialGeneric),
            new BudgetTarget(ItemClass.Service.Industrial, ItemClass.SubService.IndustrialForestry),
            new BudgetTarget(ItemClass.Service.Industrial, ItemClass.SubService.IndustrialFarming),
            new BudgetTarget(ItemClass.Service.Industrial, ItemClass.SubService.IndustrialOil),
            new BudgetTarget(ItemClass.Service.Industrial, ItemClass.SubService.IndustrialOre),
            new BudgetTarget(ItemClass.Service.Office, ItemClass.SubService.None)
        };

        public static CommandResult BuildCityHealthJson()
        {
            District city = Singleton<DistrictManager>.instance.m_districts.m_buffer[0];
            EconomyManager economy = Singleton<EconomyManager>.instance;
            StringBuilder json = new StringBuilder();
            json.Append("{\"ok\":true");
            json.Append(",\"cash\":{\"amount\":").Append(economy.LastCashAmount);
            json.Append(",\"periodNet\":").Append(economy.LastCashDelta).Append("}");
            json.Append(",\"population\":").Append(city.m_populationData.m_finalCount);
            json.Append(",\"employment\":{\"workers\":").Append(city.GetWorkerCount());
            json.Append(",\"workplaces\":").Append(city.GetWorkplaceCount());
            json.Append(",\"unemployment\":").Append(city.GetUnemployment()).Append("}");
            json.Append(",\"wellbeing\":{\"happiness\":").Append(city.m_finalHappiness);
            json.Append(",\"crimeRate\":").Append(city.m_finalCrimeRate);
            json.Append(",\"averageLifespan\":").Append(city.GetAverageLifespan());
            json.Append(",\"sick\":").Append(city.GetSickCount());
            json.Append(",\"dead\":").Append(city.GetDeadCount());
            json.Append(",\"landValue\":").Append(city.GetLandValue());
            json.Append(",\"groundPollution\":").Append(city.GetGroundPollution());
            json.Append(",\"waterPollution\":").Append(city.GetWaterPollution()).Append("}");
            json.Append(",\"utilities\":{");
            AppendCapacity(json, "electricity", city.GetElectricityConsumption(), city.GetElectricityCapacity(), true);
            AppendCapacity(json, "water", city.GetWaterConsumption(), city.GetWaterCapacity(), false);
            AppendCapacity(json, "sewage", city.GetSewageAccumulation(), city.GetSewageCapacity(), false);
            // Landfills store garbage instead of processing it: incineration capacity is 0 in a
            // landfill-only city, so the storage fill level is the shortage signal.
            AppendCapacity(json, "garbage", city.GetGarbageAccumulation(), city.GetIncinerationCapacity(), false,
                ItemClass.Service.Garbage, city.GetGarbageAmount(), city.GetGarbageCapacity());
            json.Append("}");
            json.Append(",\"services\":{");
            AppendCapacity(json, "healthcare", city.GetSickCount(), city.GetHealCapacity(), true);
            AppendCapacity(json, "deathcare", city.GetDeadAmount(), city.GetCremateCapacity(), false,
                ItemClass.Service.HealthCare, city.GetDeadAmount(), city.GetDeadCapacity());
            AppendCapacity(json, "elementaryEducation", city.GetEducation1Need(), city.GetEducation1Capacity(), false);
            AppendCapacity(json, "highSchoolEducation", city.GetEducation2Need(), city.GetEducation2Capacity(), false);
            AppendCapacity(json, "universityEducation", city.GetEducation3Need(), city.GetEducation3Capacity(), false);
            AppendCapacity(json, "police", city.GetCriminalAmount(), city.GetCriminalCapacity(), false);
            json.Append("}");
            json.Append(",\"trade\":{\"imports\":").Append(city.GetImportAmount());
            json.Append(",\"exports\":").Append(city.GetExportAmount()).Append("}}");
            return CommandResult.FromJson(json.ToString());
        }

        public static CommandResult BuildBudgetsJson()
        {
            EconomyManager economy = Singleton<EconomyManager>.instance;
            StringBuilder items = new StringBuilder();
            for (int i = 0; i < BudgetTargets.Length; i++)
            {
                BudgetTarget target = BudgetTargets[i];
                int day = economy.GetBudget(target.Service, target.SubService, false);
                int night = economy.GetBudget(target.Service, target.SubService, true);
                if (i > 0) items.Append(",");
                items.Append("{\"service\":\"").Append(target.Service).Append("\"");
                items.Append(",\"subService\":\"").Append(target.SubService).Append("\"");
                items.Append(",\"day\":").Append(day).Append(",\"night\":").Append(night).Append("}");
            }

            return CommandResult.FromJson("{\"ok\":true,\"budgets\":[" + items.ToString() + "]}");
        }

        public static CommandResult BuildEconomyDetailsJson()
        {
            EconomyManager economy = Singleton<EconomyManager>.instance;
            StringBuilder items = new StringBuilder();
            long totalIncome = 0;
            long totalExpenses = 0;
            for (int i = 0; i < BudgetTargets.Length; i++)
            {
                BudgetTarget target = BudgetTargets[i];
                long income;
                long expenses;
                economy.GetIncomeAndExpenses(target.Service, target.SubService, ItemClass.Level.None, out income, out expenses);
                if (i > 0) items.Append(",");
                items.Append("{\"service\":\"").Append(target.Service).Append("\"");
                items.Append(",\"subService\":\"").Append(target.SubService).Append("\"");
                items.Append(",\"income\":").Append(income);
                items.Append(",\"expenses\":").Append(expenses);
                items.Append(",\"net\":").Append(income - expenses).Append("}");
                totalIncome += income;
                totalExpenses += expenses;
            }

            StringBuilder taxes = new StringBuilder();
            long taxIncome = 0;
            for (int i = 0; i < TaxTargets.Length; i++)
            {
                BudgetTarget target = TaxTargets[i];
                long income;
                long expenses;
                economy.GetIncomeAndExpenses(target.Service, target.SubService, ItemClass.Level.None, out income, out expenses);
                if (i > 0) taxes.Append(",");
                taxes.Append("{\"service\":\"").Append(target.Service).Append("\"");
                taxes.Append(",\"subService\":\"").Append(target.SubService).Append("\"");
                taxes.Append(",\"income\":").Append(income).Append("}");
                taxIncome += income;
            }
            totalIncome += taxIncome;
            long loanExpenses = economy.GetLoanExpenses();
            long policyExpenses = economy.GetPolicyExpenses();

            StringBuilder loans = new StringBuilder();
            int loanCount = economy.CountLoans();
            for (int i = 0; i < loanCount; i++)
            {
                EconomyManager.Loan loan;
                if (!economy.GetLoan(i, out loan)) continue;
                if (loans.Length > 0) loans.Append(",");
                loans.Append("{\"index\":").Append(i);
                loans.Append(",\"amountTaken\":").Append(loan.m_amountTaken);
                loans.Append(",\"amountLeft\":").Append(loan.m_amountLeft);
                loans.Append(",\"interestRate\":").Append(loan.m_interestRate);
                loans.Append(",\"interestPaid\":").Append(loan.m_interestPaid);
                loans.Append(",\"length\":").Append(loan.m_length).Append("}");
            }

            return CommandResult.FromJson("{\"ok\":true,\"cash\":{\"amount\":" + economy.LastCashAmount +
                ",\"periodNet\":" + economy.LastCashDelta +
                "},\"totals\":{\"income\":" + totalIncome +
                ",\"taxIncome\":" + taxIncome +
                ",\"expenses\":" + totalExpenses +
                ",\"net\":" + (totalIncome - totalExpenses) +
                ",\"loanExpenses\":" + loanExpenses +
                ",\"policyExpenses\":" + policyExpenses +
                // Rows read the current accounting period while periodNet is the
                // last completed change, so a gap is expected but should be small.
                ",\"periodNetMinusRowNet\":" + (economy.LastCashDelta - (totalIncome - totalExpenses - loanExpenses - policyExpenses)) +
                "},\"taxes\":[" + taxes.ToString() +
                "],\"services\":[" + items.ToString() +
                "],\"loans\":[" + loans.ToString() + "]}");
        }

        public static CommandResult BuildLoanOptionsJson()
        {
            EconomyManager economy = Singleton<EconomyManager>.instance;
            StringBuilder slots = new StringBuilder();
            for (int slot = 0; slot < EconomyManager.MAX_LOANS; slot++)
            {
                EconomyManager.Loan active;
                bool hasActive = economy.GetLoan(slot, out active) && active.m_amountLeft > 0;
                EconomyManager.LoanInfo[] options;
                bool available = economy.GetLoanInfo(slot, out options);
                if (slot > 0) slots.Append(",");
                slots.Append("{\"slot\":").Append(slot);
                slots.Append(",\"available\":").Append(JsonUtil.Bool(available));
                slots.Append(",\"active\":").Append(JsonUtil.Bool(hasActive));
                if (hasActive)
                {
                    slots.Append(",\"activeLoan\":{\"amountTaken\":").Append(active.m_amountTaken);
                    slots.Append(",\"amountLeft\":").Append(active.m_amountLeft);
                    slots.Append(",\"interestRate\":").Append(active.m_interestRate);
                    slots.Append(",\"interestPaid\":").Append(active.m_interestPaid);
                    slots.Append(",\"length\":").Append(active.m_length).Append("}");
                }
                slots.Append(",\"options\":[");
                if (options != null)
                {
                    for (int option = 0; option < options.Length; option++)
                    {
                        if (option > 0) slots.Append(",");
                        EconomyManager.LoanInfo info = options[option];
                        slots.Append("{\"index\":").Append(option);
                        slots.Append(",\"amount\":").Append(info.m_amount);
                        slots.Append(",\"interest\":").Append(info.m_interest);
                        slots.Append(",\"length\":").Append(info.m_length).Append("}");
                    }
                }
                slots.Append("]}");
            }
            return CommandResult.FromJson("{\"ok\":true,\"slots\":[" + slots.ToString() + "]}");
        }

        public static CommandResult TakeLoan(string body)
        {
            int slot = (int)JsonUtil.GetNumber(body, "slot", -1f);
            int option = (int)JsonUtil.GetNumber(body, "option", 0f);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            if (slot < 0 || slot >= EconomyManager.MAX_LOANS) return CommandResult.Fail("slot is outside the available loan range.");

            EconomyManager economy = Singleton<EconomyManager>.instance;
            EconomyManager.Loan active;
            if (economy.GetLoan(slot, out active) && active.m_amountLeft > 0)
            {
                return CommandResult.Fail("Loan slot already has an outstanding balance: " + slot);
            }
            EconomyManager.LoanInfo[] options;
            if (!economy.GetLoanInfo(slot, out options) || options == null || options.Length == 0)
            {
                return CommandResult.Fail("Loan slot is not currently available: " + slot);
            }
            if (option < 0 || option >= options.Length) return CommandResult.Fail("option is outside the available loan option range.");
            EconomyManager.LoanInfo selected = options[option];

            if (!dryRun)
            {
                System.Collections.Generic.IEnumerator<bool> operation =
                    economy.TakeNewLoan(slot, selected.m_amount, selected.m_interest, selected.m_length);
                while (operation.MoveNext()) { }
            }
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"slot\":" + slot + ",\"option\":" + option +
                ",\"amount\":" + selected.m_amount +
                ",\"interest\":" + selected.m_interest +
                ",\"length\":" + selected.m_length + "}");
        }

        public static CommandResult PayLoan(string body)
        {
            int slot = (int)JsonUtil.GetNumber(body, "slot", -1f);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            if (slot < 0 || slot >= EconomyManager.MAX_LOANS) return CommandResult.Fail("slot is outside the available loan range.");
            EconomyManager economy = Singleton<EconomyManager>.instance;
            EconomyManager.Loan loan;
            if (!economy.GetLoan(slot, out loan) || loan.m_amountLeft <= 0)
            {
                return CommandResult.Fail("No outstanding loan exists in slot: " + slot);
            }
            if (!dryRun)
            {
                System.Collections.Generic.IEnumerator<bool> operation = economy.PayLoanNow(slot);
                while (operation.MoveNext()) { }
            }
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"slot\":" + slot + ",\"amountLeftBefore\":" + loan.m_amountLeft + "}");
        }

        public static CommandResult SetServiceBudget(string body)
        {
            string serviceText = JsonUtil.GetString(body, "service", "");
            string subServiceText = JsonUtil.GetString(body, "subService", "");
            int budget = (int)JsonUtil.GetNumber(body, "budget", -1f);
            bool night = JsonUtil.GetBool(body, "night", false);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            ItemClass.Service service;
            ItemClass.SubService subService;
            if (!TryEnum(serviceText, out service)) return CommandResult.Fail("Unknown service: " + serviceText);
            if (!TryEnum(subServiceText, out subService)) return CommandResult.Fail("Unknown subService: " + subServiceText);
            if (budget < 50 || budget > 150) return CommandResult.Fail("budget must be between 50 and 150.");

            EconomyManager economy = Singleton<EconomyManager>.instance;
            int before = economy.GetBudget(service, subService, night);
            if (!dryRun) economy.SetBudget(service, subService, budget, night);
            int after = dryRun ? before : economy.GetBudget(service, subService, night);
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"service\":\"" + service + "\",\"subService\":\"" + subService +
                "\",\"night\":" + JsonUtil.Bool(night) +
                ",\"before\":" + before + ",\"after\":" + after + "}");
        }

        public static CommandResult BuildPoliciesJson(int districtId)
        {
            DistrictManager districts = Singleton<DistrictManager>.instance;
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            bool districtScope = districtId > 0;
            if (districtScope && (districtId >= 128 || (districts.m_districts.m_buffer[districtId].m_flags & District.Flags.Created) == District.Flags.None))
            {
                return CommandResult.Fail("District was not found: " + districtId);
            }

            StringBuilder items = new StringBuilder();
            bool first = true;
            Array policies = Enum.GetValues(typeof(DistrictPolicies.Policies));
            for (int i = 0; i < policies.Length; i++)
            {
                DistrictPolicies.Policies policy = (DistrictPolicies.Policies)policies.GetValue(i);
                if (policy == DistrictPolicies.Policies.None) continue;
                bool active = districtScope
                    ? districts.IsDistrictPolicySet(policy, (byte)districtId)
                    : districts.IsCityPolicySet(policy);
                bool unlocked = false;
                try { unlocked = unlocks.Unlocked(policy); } catch { }
                if (!first) items.Append(",");
                items.Append("{\"name\":\"").Append(policy).Append("\"");
                items.Append(",\"value\":").Append((int)policy);
                items.Append(",\"unlocked\":").Append(JsonUtil.Bool(unlocked));
                items.Append(",\"active\":").Append(JsonUtil.Bool(active)).Append("}");
                first = false;
            }

            return CommandResult.FromJson("{\"ok\":true,\"scope\":\"" + (districtScope ? "district" : "city") +
                "\",\"districtId\":" + districtId + ",\"policies\":[" + items.ToString() + "]}");
        }

        public static CommandResult SetPolicy(string body)
        {
            string name = JsonUtil.GetString(body, "policy", "");
            int districtId = (int)JsonUtil.GetNumber(body, "districtId", 0f);
            bool active = JsonUtil.GetBool(body, "active", true);
            bool dryRun = JsonUtil.GetBool(body, "dryRun", false);
            DistrictPolicies.Policies policy;
            if (!TryEnum(name, out policy) || policy == DistrictPolicies.Policies.None) return CommandResult.Fail("Unknown policy: " + name);
            UnlockManager unlocks = Singleton<UnlockManager>.instance;
            if (active && !unlocks.Unlocked(policy)) return CommandResult.Fail("Policy is still locked: " + policy);

            DistrictManager districts = Singleton<DistrictManager>.instance;
            if (districtId > 0 && (districtId >= 128 || (districts.m_districts.m_buffer[districtId].m_flags & District.Flags.Created) == District.Flags.None))
            {
                return CommandResult.Fail("District was not found: " + districtId);
            }

            bool before = districtId > 0
                ? districts.IsDistrictPolicySet(policy, (byte)districtId)
                : districts.IsCityPolicySet(policy);
            if (!dryRun)
            {
                int targetId = districtId > 0 ? districtId : 0;
                District target = districts.m_districts.m_buffer[targetId];
                if (active) target.SetPolicy(policy);
                else target.UnsetPolicy(policy);
                districts.m_districts.m_buffer[targetId] = target;
            }
            bool after = dryRun ? before : (districtId > 0
                ? districts.IsDistrictPolicySet(policy, (byte)districtId)
                : districts.IsCityPolicySet(policy));
            return CommandResult.FromJson("{\"ok\":true,\"dryRun\":" + JsonUtil.Bool(dryRun) +
                ",\"policy\":\"" + policy + "\",\"districtId\":" + districtId +
                ",\"before\":" + JsonUtil.Bool(before) + ",\"after\":" + JsonUtil.Bool(after) + "}");
        }

        private static void AppendCapacity(StringBuilder json, string name, int used, int capacity, bool first)
        {
            AppendCapacity(json, name, used, capacity, first, ItemClass.Service.None, -1, -1);
        }

        private static void AppendCapacity(StringBuilder json, string name, int used, int capacity, bool first,
            ItemClass.Service storageService, int storedAmount, int storageCapacity)
        {
            if (!first) json.Append(",");
            json.Append("\"").Append(name).Append("\":{\"used\":").Append(used);
            json.Append(",\"capacity\":").Append(capacity);
            json.Append(",\"utilization\":").Append(JsonUtil.Number(Utilization(used, capacity)));
            if (storageCapacity >= 0) json.Append(",\"storage\":").Append(StorageJson(storageService, storedAmount, storageCapacity));
            json.Append("}");
        }

        internal static float Utilization(int used, int capacity)
        {
            return capacity <= 0 ? (used > 0 ? 1f : 0f) : (float)used / capacity;
        }

        // Landfill or cemetery fill level: stored amount against the combined storage of all facilities.
        // A full facility stops collecting, so the facility counts expose headroom that sits in only a
        // few facilities even when the combined fill level looks safe.
        internal static string StorageJson(ItemClass.Service service, int storedAmount, int storageCapacity)
        {
            int facilities;
            int fullFacilities;
            CountStorageFacilities(service, out facilities, out fullFacilities);
            return "{\"amount\":" + storedAmount + ",\"capacity\":" + storageCapacity +
                ",\"utilization\":" + JsonUtil.Number(Utilization(storedAmount, storageCapacity)) +
                ",\"facilities\":" + facilities + ",\"fullFacilities\":" + fullFacilities + "}";
        }

        private static void CountStorageFacilities(ItemClass.Service service, out int facilities, out int fullFacilities)
        {
            facilities = 0;
            fullFacilities = 0;
            Building[] buffer = Singleton<BuildingManager>.instance.m_buildings.m_buffer;
            for (int i = 1; i < buffer.Length; i++)
            {
                if ((buffer[i].m_flags & (Building.Flags.Created | Building.Flags.Deleted)) != Building.Flags.Created) continue;
                BuildingInfo info = buffer[i].Info;
                if (info == null || info.m_class == null || info.m_class.m_service != service) continue;
                if (!(info.m_buildingAI is LandfillSiteAI) && !(info.m_buildingAI is CemeteryAI)) continue;
                facilities++;
                // CapacityFull is CapacityStep1 | CapacityStep2; a single step bit means partly full.
                if ((buffer[i].m_flags & Building.Flags.CapacityFull) == Building.Flags.CapacityFull) fullFacilities++;
            }
        }

        private struct BudgetTarget
        {
            public readonly ItemClass.Service Service;
            public readonly ItemClass.SubService SubService;

            public BudgetTarget(ItemClass.Service service, ItemClass.SubService subService)
            {
                Service = service;
                SubService = subService;
            }
        }

        private static bool TryEnum<T>(string value, out T result)
        {
            try
            {
                result = (T)Enum.Parse(typeof(T), value, true);
                return true;
            }
            catch
            {
                result = default(T);
                return false;
            }
        }
    }
}
