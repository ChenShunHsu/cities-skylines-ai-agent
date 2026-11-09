#!/usr/bin/env python3

import copy
import json
import sys
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from mayor import contracts


class LocalExecutorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(
            (SCRIPTS / "fixtures" / "local-executor-contract-v1.json").read_text(
                encoding="utf-8"
            )
        )
        cls.tool_names = frozenset(contracts.repository_tool_action_kinds())

    def setUp(self):
        self.mission = copy.deepcopy(self.fixture["mission"])
        self.context = contracts.ExecutorContext(
            instance_id="fixture-city-instance",
            tool_contract_version=contracts.repository_tool_contract_version(),
            registered_tool_names=self.tool_names,
            game_time=datetime.fromisoformat("2035-06-01T09:00:00"),
            completed_steps=7,
            spent_display=0,
        )

    def test_repository_tool_policy_covers_every_typed_tool(self):
        policies = contracts.repository_tool_action_kinds()
        definitions = contracts.repository_tool_definitions()
        self.assertEqual(
            {item["function"]["name"] for item in definitions}, set(policies)
        )
        self.assertEqual("read_only", policies["inspect_traffic"])
        self.assertEqual("bulldoze", policies["bulldoze_entity"])

    def test_contract_schema_enums_stay_aligned(self):
        mission_schema = contracts.load_contract_schema("mission")
        action_schema = contracts.load_contract_schema("action")
        progress_schema = contracts.load_contract_schema("progress")
        escalation_schema = contracts.load_contract_schema("escalation")
        action_kinds = {
            value for value in contracts.repository_tool_action_kinds().values()
            if value != "read_only"
        }
        self.assertEqual(
            action_kinds,
            set(mission_schema["properties"]["forbiddenActions"]["items"]["enum"]),
        )
        mission_escalations = set(
            mission_schema["properties"]["escalateWhen"]["items"]["enum"]
        )
        action_reasons = set(action_schema["properties"]["reasonCode"]["enum"])
        escalation_reasons = set(
            escalation_schema["properties"]["blockerCode"]["enum"]
        )
        self.assertLessEqual(mission_escalations, action_reasons)
        self.assertEqual(mission_escalations, escalation_reasons)
        mission_metrics = set(
            mission_schema["properties"]["successConditions"]["items"]
            ["properties"]["metric"]["enum"]
        )
        progress_metrics = set(
            progress_schema["properties"]["observations"]["items"]
            ["properties"]["metric"]["enum"]
        )
        before_metrics = set(
            escalation_schema["properties"]["before"]["items"]
            ["properties"]["metric"]["enum"]
        )
        after_metrics = set(
            escalation_schema["properties"]["after"]["items"]
            ["properties"]["metric"]["enum"]
        )
        self.assertEqual(mission_metrics, progress_metrics)
        self.assertEqual(mission_metrics, before_metrics)
        self.assertEqual(mission_metrics, after_metrics)

    def test_valid_fixture_contracts_are_accepted(self):
        contracts.validate_mission_contract(self.mission, self.context)
        contracts.validate_action_envelope(
            copy.deepcopy(self.fixture["action"]), self.mission, self.context
        )
        contracts.validate_progress_summary(
            copy.deepcopy(self.fixture["progress"]), self.mission, self.context
        )
        contracts.validate_escalation_summary(
            copy.deepcopy(self.fixture["escalation"]), self.mission, self.context
        )

    def test_model_document_parser_rejects_malformed_duplicate_and_oversize_output(self):
        action = json.dumps(self.fixture["action"], ensure_ascii=False)
        self.assertEqual(
            self.fixture["action"], contracts.parse_model_document(action, "action")
        )
        with self.assertRaisesRegex(ValueError, "malformed JSON"):
            contracts.parse_model_document(action + " trailing", "action")
        with self.assertRaisesRegex(ValueError, "duplicate key"):
            contracts.parse_model_document('{"envelopeVersion":1,"envelopeVersion":1}', "action")
        with self.assertRaisesRegex(ValueError, "character limit"):
            contracts.parse_model_document(
                " " * (contracts.MAX_MODEL_OUTPUT_CHARS + 1), "action"
            )

    def test_scope_and_tool_contract_mismatch_fail_closed(self):
        for field, value, message in (
            ("instanceId", "other-city", "live city instance"),
            ("toolContractVersion", "stale-tools", "toolContractVersion"),
        ):
            with self.subTest(field=field):
                mission = copy.deepcopy(self.mission)
                mission[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    contracts.validate_mission_contract(mission, self.context)

    def test_expired_contract_and_exhausted_steps_fail_before_action(self):
        expired = copy.deepcopy(self.mission)
        expired["expiresAtGameTime"] = "2035-06-01T08:00:00"
        with self.assertRaisesRegex(ValueError, "expired"):
            contracts.validate_mission_contract(expired, self.context)

        exhausted_context = replace(self.context, completed_steps=20)
        with self.assertRaisesRegex(ValueError, "step budget"):
            contracts.validate_mission_contract(self.mission, exhausted_context)

    def test_unknown_duplicate_and_forbidden_tools_are_rejected(self):
        unknown = copy.deepcopy(self.mission)
        unknown["allowedToolNames"].append("invented_tool")
        with self.assertRaisesRegex(ValueError, "unregistered tools"):
            contracts.validate_mission_contract(unknown, self.context)

        duplicate = copy.deepcopy(self.mission)
        duplicate["allowedToolNames"].append("observe_city")
        with self.assertRaisesRegex(ValueError, "unique"):
            contracts.validate_mission_contract(duplicate, self.context)

        contradiction = copy.deepcopy(self.mission)
        contradiction["allowedToolNames"].append("set_tax_rate")
        with self.assertRaisesRegex(ValueError, "contradicts forbidden"):
            contracts.validate_mission_contract(contradiction, self.context)

    def test_action_rejects_tool_outside_allowlist_and_bad_arguments(self):
        outside = copy.deepcopy(self.fixture["action"])
        outside["toolName"] = "inspect_economy"
        outside["arguments"] = {}
        with self.assertRaisesRegex(ValueError, "outside the mission allowlist"):
            contracts.validate_action_envelope(outside, self.mission, self.context)

        malformed = copy.deepcopy(self.fixture["action"])
        malformed["arguments"] = {"limit": 501}
        with self.assertRaisesRegex(ValueError, "maximum 500"):
            contracts.validate_action_envelope(malformed, self.mission, self.context)

    def test_action_rejects_nonsequential_step_and_budget_overrun(self):
        nonsequential = copy.deepcopy(self.fixture["action"])
        nonsequential["step"] = 9
        with self.assertRaisesRegex(ValueError, "next sequential"):
            contracts.validate_action_envelope(nonsequential, self.mission, self.context)

        over_budget = copy.deepcopy(self.fixture["action"])
        over_budget["estimatedCostDisplay"] = 1
        with self.assertRaisesRegex(ValueError, "display-cost budget"):
            contracts.validate_action_envelope(over_budget, self.mission, self.context)

    def test_non_tool_decision_has_closed_shape_and_declared_escalation(self):
        escalation = {
            "envelopeVersion": 1,
            "missionId": self.mission["missionId"],
            "instanceId": self.mission["instanceId"],
            "toolContractVersion": self.mission["toolContractVersion"],
            "step": 8,
            "decision": "escalate",
            "reasonCode": "missing_required_telemetry",
        }
        contracts.validate_action_envelope(escalation, self.mission, self.context)

        escalation["toolName"] = "inspect_traffic"
        with self.assertRaisesRegex(ValueError, "must not include tool-call fields"):
            contracts.validate_action_envelope(escalation, self.mission, self.context)

        undeclared = copy.deepcopy(self.mission)
        undeclared["escalateWhen"].remove("missing_required_telemetry")
        escalation.pop("toolName")
        with self.assertRaisesRegex(ValueError, "not declared"):
            contracts.validate_action_envelope(escalation, undeclared, self.context)

    def test_complete_and_wait_require_bounded_reason_codes(self):
        envelope = {
            "envelopeVersion": 1,
            "missionId": self.mission["missionId"],
            "instanceId": self.mission["instanceId"],
            "toolContractVersion": self.mission["toolContractVersion"],
            "step": 8,
            "decision": "complete",
            "reasonCode": "stage_complete",
        }
        with self.assertRaisesRegex(ValueError, "mission_complete"):
            contracts.validate_action_envelope(envelope, self.mission, self.context)
        envelope["decision"] = "wait"
        envelope["reasonCode"] = "missing_required_telemetry"
        with self.assertRaisesRegex(ValueError, "bounded wait reason"):
            contracts.validate_action_envelope(envelope, self.mission, self.context)

    def test_progress_summary_reconciles_controller_budgets_and_refs(self):
        progress = copy.deepcopy(self.fixture["progress"])
        progress["remainingSteps"] = 12
        with self.assertRaisesRegex(ValueError, "remainingSteps"):
            contracts.validate_progress_summary(progress, self.mission, self.context)

        progress = copy.deepcopy(self.fixture["progress"])
        progress["evidenceRefs"] = ["C:/private/runtime/result.json"]
        with self.assertRaisesRegex(ValueError, "artifact://"):
            contracts.validate_progress_summary(progress, self.mission, self.context)

        progress = copy.deepcopy(self.fixture["progress"])
        progress["currentStage"] = "verify"
        with self.assertRaisesRegex(ValueError, "currentStage"):
            contracts.validate_progress_summary(progress, self.mission, self.context)

    def test_terminal_summary_can_report_exhausted_contract(self):
        exhausted_context = contracts.ExecutorContext(
            instance_id=self.context.instance_id,
            tool_contract_version=self.context.tool_contract_version,
            registered_tool_names=self.context.registered_tool_names,
            game_time="2035-06-01T12:00:00",
            completed_steps=20,
            spent_display=0,
        )
        escalation = copy.deepcopy(self.fixture["escalation"])
        escalation["executorSteps"] = 20
        escalation["blockerCode"] = "step_budget_exhausted"
        contracts.validate_escalation_summary(escalation, self.mission, exhausted_context)


if __name__ == "__main__":
    unittest.main()
