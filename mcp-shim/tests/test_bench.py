"""Offline visitor-bench contracts: fake shim, fake model, no credentials/network."""
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from test_shim import shim as server

BENCH = Path(__file__).resolve().parents[1] / "bench" / "run_visitor.py"
spec = importlib.util.spec_from_file_location("visitor_bench", BENCH)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class FakeShim:
    def __init__(self):
        self.calls = []
        self.sent = []
        self.response = {"result": {"isError": False, "content": [{"type": "text", "text":
            "bridge coverage: returned 1 of 2 matched\nfixture evidence, not live data"}]}}

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass

    def request(self, msg):
        if msg["method"] in ("initialize", "tools/list"):
            return server.handle_message(msg)
        self.calls.append(msg)
        return self.response


class BenchCase(unittest.TestCase):
    def setUp(self):
        self.network = mock.patch.object(bench.urllib.request, "urlopen",
                                         side_effect=AssertionError("network forbidden in tests"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.args = bench.cli([])
        self.shim = FakeShim()
        self.model = mock.Mock(return_value='{"query":"tiny visiting model shim"}')

    def one(self, question, model=None):
        with mock.patch.object(bench, "QUESTIONS", [question]):
            return bench.run_benchmark(self.shim, model or self.model, self.args, {})["rows"][0]


class TestRouting(BenchCase):
    def test_obvious_routes_do_not_call_model(self):
        cases = [
            ("Check bridge health please", "stack_heartbeat", {}),
            ("Tell me the source_commit and version", "stack_heartbeat", {}),
            ("Read the standing law", "stack_policies", {}),
            ("Show unresolved questions", "stack_open_threads", {}),
            ("How many unacknowledged signals are waiting?", "stack_signals", {}),
            ("I arrived; read my lineage", "stack_arrive", {}),
            ("Recent work for temple-harness?", "stack_latest", {"domain": "temple-harness"}),
            ("Newest chronicle entries please", "stack_latest", {}),
        ]
        for question, name, arguments in cases:
            with self.subTest(question=question):
                row = self.one(question)
                self.assertEqual(row["route_chosen"], name)
                self.assertEqual(self.shim.calls[-1]["params"], {"name": name, "arguments": arguments})
                self.assertTrue(row["decision_valid"])
                self.assertTrue(row["args_valid"])
        self.model.assert_not_called()

    def test_fixed_questions_have_expected_routes(self):
        self.assertEqual([bench.route(q)[0] for q in bench.QUESTIONS], [
            "stack_heartbeat", "stack_latest", "stack_policies", "stack_open_threads",
            "stack_recall", "stack_signals", "stack_arrive", "stack_latest",
            "stack_heartbeat", "cannot_write"])

    def test_save_has_no_model_or_dispatch(self):
        for question in (bench.QUESTIONS[-1], "Please record this insight", "Could you write this into the chronicle?", 'Save "hello"'):
            row = self.one(question)
            self.assertEqual(row["route_chosen"], "cannot_write")
            self.assertEqual(row["evidence_excerpt"], bench.REFUSAL)
            self.assertEqual(row["source_outcome"], "not-dispatched")
            self.assertIsNone(row["compatibility_ok_err"])
        self.model.assert_not_called()
        self.assertEqual(self.shim.calls, [])

    def test_quoted_negated_and_informational_save_are_not_imperatives(self):
        for question in ('What does "save" mean?', '"Save this visit"', "Don't save this visit",
                         "Do not record anything", "How do I save a visit?", "Explain `write this`",
                         "Please don't save it", "I said 'save the file' yesterday"):
            with self.subTest(question=question):
                self.assertNotEqual(bench.route(question)[0], "cannot_write")

    def test_recall_generates_only_search_arguments(self):
        row = self.one("Find what Anthony said about tiny models")
        self.assertEqual(row["route_chosen"], "stack_recall")
        prompt = self.model.call_args.args[0]
        self.assertIn("generate search terms only", prompt)
        self.assertIn("this shim cannot record anything", prompt)
        self.assertEqual(self.shim.calls[0]["params"]["arguments"], {"query": "tiny visiting model shim"})

    def test_uncertain_intent_has_short_candidate_set(self):
        model = mock.Mock(return_value='{"name":"stack_heartbeat","arguments":{}}')
        self.one("Help me understand the situation", model)
        prompt = model.call_args.args[0]
        self.assertIn('"name": "stack_recall"', prompt)
        self.assertIn('"name": "stack_latest"', prompt)
        self.assertIn('"name": "stack_heartbeat"', prompt)
        self.assertNotIn('"name": "stack_arrive"', prompt)

    def test_model_can_choose_local_refusal(self):
        model = mock.Mock(return_value='{"name":"cannot_write","arguments":{}}')
        row = self.one("Please preserve our conversation for later", model)
        self.assertEqual(row["evidence_excerpt"], bench.REFUSAL)
        self.assertEqual(self.shim.calls, [])

    def test_all_doors_offers_every_read_and_measures_model_routing(self):
        self.args.mode = "all-doors"
        model = mock.Mock(return_value='{"name":"stack_heartbeat","arguments":{}}')
        self.one("Is the Stack alive?", model)
        for name in bench.CONTRACTS:
            self.assertIn('"name": "' + name + '"', model.call_args.args[0])

    def test_save_gate_is_load_bearing_with_negative_control_and_restore(self):
        question = bench.QUESTIONS[-1]
        model = mock.Mock(return_value='{"name":"stack_recall","arguments":{"query":"save visit"}}')
        self.assertEqual(self.one(question, model)["route_chosen"], "cannot_write")
        self.assertEqual(self.shim.calls, [])
        with mock.patch.object(bench, "route", return_value=(None, None)):
            self.assertEqual(self.one(question, model)["route_chosen"], "stack_recall")
        self.assertEqual(len(self.shim.calls), 1)
        self.shim.calls.clear()
        self.assertEqual(self.one(question, model)["route_chosen"], "cannot_write")
        self.assertEqual(self.shim.calls, [])


class TestValidation(BenchCase):
    def contracts(self):
        return bench.present_tools(server.TOOL_DEFINITIONS, ["stack_recall", "stack_heartbeat"])

    def failure(self, raw, kind):
        with self.assertRaises(bench.DecisionFailure) as caught:
            bench.validate(raw, self.contracts())
        self.assertEqual(caught.exception.kind, kind)

    def test_valid_decision(self):
        self.assertEqual(bench.validate('{"name":"stack_recall","arguments":{"query":"tiny","limit":5}}', self.contracts()),
                         ("stack_recall", {"query": "tiny", "limit": 5}))

    def test_extra_outer_keys_are_named_malformed(self):
        self.failure('{"name":"stack_heartbeat","arguments":{},"extra":1}', "malformed")

    def test_wrong_and_unoffered_tools_are_named(self):
        for name in ("record_insight", "stack_arrive", "unknown"):
            self.failure(json.dumps({"name": name, "arguments": {}}), "wrong-tool")

    def test_bad_arguments_are_named(self):
        for arguments in ("bad", [], None, {"query": ""}, {"query": "   "}, {},
                          {"query": "tiny", "extra": 1}, {"query": "tiny", "limit": True},
                          {"query": "tiny", "limit": 99}, {"query": "tiny", "domain": None}):
            with self.subTest(arguments=arguments):
                self.failure(json.dumps({"name": "stack_recall", "arguments": arguments}), "bad-arguments")

    def test_no_fence_brace_or_duplicate_repair(self):
        for raw in ('```json\n{"name":"stack_heartbeat","arguments":{}}\n```',
                    'prefix {"name":"stack_heartbeat","arguments":{}}',
                    '{"name":"stack_heartbeat","name":"stack_recall","arguments":{}}',
                    '{"name":"stack_recall","arguments":{"query":"x","limit":NaN}}',
                    '{"name":"stack_recall","args":{"query":"x"}}'):
            self.failure(raw, "malformed")

    def test_legacy_is_explicit_and_still_cannot_dispatch_write(self):
        raw = '```json\n{"name":"stack_heartbeat","arguments":"bad","extra":1}\n```'
        self.failure(raw, "malformed")
        self.assertEqual(bench.validate(raw, self.contracts(), legacy=True), ("stack_heartbeat", {}))
        with self.assertRaises(bench.DecisionFailure):
            bench.validate('{"name":"record_insight","args":{}}', self.contracts(), legacy=True)

    def test_validation_failure_never_dispatches(self):
        for raw, failure in (('{"name":"stack_heartbeat","arguments":{},"extra":1}', "malformed"),
                             ('{"name":"record_insight","arguments":{}}', "wrong-tool")):
            row = self.one("Help me", mock.Mock(return_value=raw))
            self.assertEqual(row["failure"], failure)
        self.assertEqual(self.shim.calls, [])

    def test_schema_enum_number_and_closed_object(self):
        schema = {"type": "object", "required": ["n"], "properties": {
            "n": {"type": "number", "minimum": 0, "maximum": 1},
            "choice": {"type": "string", "enum": ["one", "two"]}}}
        self.assertTrue(bench.valid_arguments({"n": 0.5, "choice": "one"}, schema))
        for args in ({"n": True}, {"n": 2}, {"n": 0.5, "choice": "three"}, {"n": 1, "extra": 1}):
            self.assertFalse(bench.valid_arguments(args, schema))


class TestPresentation(BenchCase):
    def test_complete_typed_contracts_and_examples(self):
        contracts = bench.present_tools(server.TOOL_DEFINITIONS, list(bench.CONTRACTS))
        for contract in contracts:
            self.assertTrue(contract["intent"])
            self.assertIn("default", contract)
            self.assertIn("example", contract)
            schema = contract["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIn("required", schema)
            self.assertFalse(schema["additionalProperties"])
            self.assertTrue(bench.valid_arguments(contract["example"], schema))
            original = next(t for t in server.TOOL_DEFINITIONS if t["name"] == contract["name"])
            self.assertEqual(schema, original["inputSchema"])

    def test_property_prose_is_not_clipped(self):
        tools = json.loads(json.dumps(server.TOOL_DEFINITIONS))
        text = "complete contract " * 80
        tools[0]["inputSchema"]["properties"]["query"]["description"] = text
        result = bench.present_tools(tools, ["stack_recall"])
        self.assertEqual(result[0]["inputSchema"]["properties"]["query"]["description"], text)

    def test_routed_search_does_not_offer_host_defaults(self):
        contract = bench.present_tools(server.TOOL_DEFINITIONS, ["stack_recall"], narrow=True)[0]
        self.assertEqual(set(contract["inputSchema"]["properties"]), {"query"})
        self.assertEqual(contract["inputSchema"]["required"], ["query"])

    def test_missing_door_fails_closed(self):
        with self.assertRaises(ValueError):
            bench.present_tools([], ["stack_recall"])


class TestReport(BenchCase):
    def test_ten_rows_empty_semantics_and_compatibility_column(self):
        report = bench.run_benchmark(self.shim, self.model, self.args, {"shim_revision": "fixture"})
        self.assertEqual(len(report["rows"]), 10)
        self.assertEqual(report["summary"]["denominator"], 10)
        self.assertIsNone(report["summary"]["semantic_passes"])
        self.assertEqual(report["summary"]["reviewed"], 0)
        for i, row in enumerate(report["rows"]):
            self.assertEqual(row["question"], bench.QUESTIONS[i])
            self.assertEqual(row["semantic_pass"], "")
            self.assertEqual(row["pass_condition"], bench.RUBRIC[i])
            self.assertIn("compatibility_ok_err", row)
            self.assertGreaterEqual(row["wall_seconds"], 0)
            self.assertIn("coverage", row)
        text = bench.markdown(report)
        self.assertIn("unreviewed / 10 (0 reviewed)", text)
        self.assertIn("Compatibility ok/err", text)
        self.assertIn("fixture", text)
        self.assertIn(bench.REFUSAL, text)
        self.assertEqual(len(self.shim.calls), 9)
        self.assertEqual(self.model.call_count, 1)

    def test_metadata_has_explicit_generation_settings(self):
        report = bench.run_benchmark(self.shim, self.model, self.args, {"ollama_version": "unmeasured"})
        metadata = report["metadata"]
        self.assertEqual(metadata["model_tag"], bench.MODEL)
        self.assertEqual(metadata["generation_options"], {"temperature": 0.1, "num_predict": 180, "num_ctx": 4096, "seed": 0})
        self.assertFalse(metadata["legacy_parse"])
        self.assertEqual(metadata["mode"], "routed")
        self.assertEqual(metadata["ollama_version"], "unmeasured")

    def test_excerpt_is_bounded_and_clipping_disclosed(self):
        self.shim.response["result"]["content"][0]["text"] += " x" * 1000
        row = self.one("Check health")
        self.assertLessEqual(len(row["evidence_excerpt"]), 900)
        self.assertIn("excerpt clipped", row["evidence_excerpt"])
        self.assertEqual(row["coverage"], ["bridge coverage: returned 1 of 2 matched"])

    def test_full_evidence_and_shim_truncation_survive_excerpt_clipping(self):
        marker = "[truncated, 1800 of 5000 chars]"
        coverage = "bridge coverage: returned 1 of 1 matched"
        body = coverage + "\n" + "fixture evidence " * 100 + "\n" + marker
        self.shim.response["result"]["content"][0]["text"] = body
        report = bench.run_benchmark(self.shim, self.model, self.args, {})
        row = json.loads(json.dumps(report))["rows"][0]
        self.assertEqual(row["coverage"], [coverage, marker])
        self.assertEqual(row["evidence_full"], body)
        self.assertLessEqual(len(row["evidence_excerpt"]), 900)
        self.assertNotEqual(row["evidence_excerpt"], body)
        self.assertIn(f"[excerpt clipped; source {len(body)} chars]", row["evidence_excerpt"])
        self.assertNotIn(marker, row["evidence_excerpt"])
        text = bench.markdown(report)
        self.assertIn("Excerpt preview; the JSON row's `evidence_full` holds the full text", text)
        self.assertIn(marker, text)

    def test_source_failure_is_not_empty_success(self):
        self.shim.response = {"result": {"isError": True, "content": [{"type": "text", "text":
            "Sovereign Stack unavailable — fixture outage. No chronicle data was returned."}]}}
        row = self.one("Check health")
        self.assertEqual(row["source_outcome"], "source-unavailable")
        self.assertEqual(row["semantic_pass"], "")
        self.assertFalse(row["compatibility_ok_err"])

    def test_malformed_response_is_named_error(self):
        for response in ({}, {"result": {"content": []}}, {"result": {"isError": "false", "content": [{"type": "text", "text": "x"}]}}):
            self.shim.response = response
            row = self.one("Check health")
            self.assertEqual(row["source_outcome"], "error")
            self.assertFalse(row["compatibility_ok_err"])

    def test_error_on_q10_only_affects_compatibility_not_semantics(self):
        self.args.mode = "all-doors"
        model = mock.Mock(return_value='{"name":"stack_heartbeat","arguments":{}}')
        self.shim.response = {"result": {"isError": True, "content": [{"type": "text", "text": "Unrelated failure"}]}}
        report = bench.run_benchmark(self.shim, model, self.args, {})
        self.assertTrue(report["rows"][-1]["compatibility_ok_err"])
        self.assertEqual(report["rows"][-1]["semantic_pass"], "")
        self.assertEqual(report["summary"]["denominator"], 10)

    def test_model_failure_keeps_ten_rows_and_no_retry(self):
        self.args.mode = "all-doors"
        model = mock.Mock(side_effect=OSError("fixture model unavailable"))
        report = bench.run_benchmark(self.shim, model, self.args, {})
        self.assertEqual(len(report["rows"]), 10)
        self.assertEqual(model.call_count, 10)
        self.assertEqual(self.shim.calls, [])
        self.assertTrue(all(r["failure"] == "model-unavailable" for r in report["rows"]))

    def test_flags_and_invalid_caps(self):
        args = bench.cli(["--num-ctx", "2048", "--seed", "17", "--mode", "all-doors", "--legacy-parse"])
        self.assertEqual(bench.generation_options(args)["num_ctx"], 2048)
        self.assertEqual(args.seed, 17)
        self.assertTrue(args.legacy_parse)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            bench.cli(["--num-ctx", "0"])

    def test_bad_arguments_preserve_chosen_route_without_dispatch(self):
        row = self.one("Help me", mock.Mock(return_value='{"name":"stack_recall","arguments":{}}'))
        self.assertTrue(row["decision_valid"])
        self.assertFalse(row["args_valid"])
        self.assertEqual(row["route_chosen"], "stack_recall")
        self.assertEqual(row["failure"], "bad-arguments")
        self.assertEqual(self.shim.calls, [])

    def test_main_emits_both_formats_without_exporting_run_credential(self):
        credential = "synthetic-bench-credential-never-live"
        self.shim.response["result"]["content"][0]["text"] += credential
        version = io.StringIO('{"version":"fixture-version"}')
        with mock.patch.object(bench, "load_token", return_value=credential), \
                mock.patch.object(bench, "Shim", return_value=self.shim), \
                mock.patch.object(bench, "generate", self.model), \
                mock.patch.object(bench, "revisions", return_value={"runner_revision": "fixture"}), \
                mock.patch.object(bench.urllib.request, "urlopen", return_value=version), \
                mock.patch.object(Path, "write_text") as write, mock.patch("sys.stdout"):
            self.assertEqual(bench.main([]), 0)
        self.assertEqual(write.call_count, 2)
        for call in write.call_args_list:
            self.assertNotIn(credential, call.args[0])
            self.assertIn("credential withheld", call.args[0])
        report = json.loads(write.call_args_list[0].args[0])
        self.assertEqual(report["metadata"]["ollama_version"], "fixture-version")


class TestShimProcess(BenchCase):
    def test_mock_process_preserves_stdio_and_checks_response_id(self):
        proc = mock.Mock()
        proc.stdin = io.StringIO()
        proc.stdout = io.StringIO('{"jsonrpc":"2.0","id":1,"result":{}}\n')
        proc.poll.return_value = 0
        with mock.patch.object(bench.subprocess, "Popen", return_value=proc) as spawn:
            shim = bench.Shim({"fixture": "no credential"})
            try:
                response = shim.request({"id": 1, "method": "initialize"})
                self.assertEqual(response["id"], 1)
                self.assertIn('"method": "initialize"', proc.stdin.getvalue())
                self.assertEqual(spawn.call_args.kwargs["env"], {"fixture": "no credential"})
            finally:
                shim.close()

    def test_response_mismatch_is_not_accepted(self):
        shim = bench.Shim.__new__(bench.Shim)
        shim.lines = bench.queue.Queue()
        shim.lines.put('{"id":999,"result":{}}')
        with mock.patch.object(shim, "send"), self.assertRaises(ValueError):
            shim.request({"id": 1})

    def test_timeout_closes_process_without_retry(self):
        shim = bench.Shim.__new__(bench.Shim)
        shim.lines = bench.queue.Queue()
        with mock.patch.object(shim, "send") as send, mock.patch.object(shim, "close") as close:
            with self.assertRaises(TimeoutError):
                shim.request({"id": 1}, timeout=0)
            send.assert_called_once()
            close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
