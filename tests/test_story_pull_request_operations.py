from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kwikledgers_fastmcp.domains.azure_devops import services
from kwikledgers_fastmcp.domains.azure_devops.native import CAPABILITIES
from kwikledgers_fastmcp.shared.capability_gateway import CAPABILITY_CATALOG

REPOSITORY = SimpleNamespace(id="repo-1", name="accountant_backend")
SOURCE_COMMIT = "a" * 40


def make_pull_request(*, draft: bool = True, status: str = "active", commit: str = SOURCE_COMMIT,
                      repository_id: str = "repo-1", pull_request_id: int = 77,
                      target: str = "refs/heads/stage-pre-prod") -> SimpleNamespace:
    return SimpleNamespace(
        pull_request_id=pull_request_id, title="KL-1 PR", is_draft=draft, status=status,
        source_ref_name="refs/heads/hotfix/KL-1-x", target_ref_name=target, merge_status="succeeded",
        last_merge_source_commit=SimpleNamespace(commit_id=commit), artifact_id=None, remote_url=None,
        repository=SimpleNamespace(id=repository_id, name="accountant_backend"),
    )


def make_cherry_pick(status: str, *, conflict: bool = False, message: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(cherry_pick_id=9, status=status, detailed_status=SimpleNamespace(
        conflict=conflict, failure_message=message, timedout=False))


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.git = MagicMock()
        self.wit = MagicMock()
        client = SimpleNamespace(clients=SimpleNamespace(
            get_git_client=lambda: self.git, get_work_item_tracking_client=lambda: self.wit))
        for target, value in (
            ("get_client", lambda: client), ("get_project", lambda: "Kwik Ledgers"),
            ("_resolve_git_repository", lambda git, name: REPOSITORY),
            ("_build_pull_request_browser_url", lambda name, identifier: f"https://azure/{name}/{identifier}"),
        ):
            patcher = patch.object(services, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class CatalogTests(unittest.TestCase):
    def test_capabilities_are_registered_with_expected_operations(self) -> None:
        operations = {definition.name: definition.operation for definition in CAPABILITY_CATALOG}
        self.assertEqual(operations["publish_story_pull_request"], "execute")
        self.assertEqual(operations["cherry_pick_story_pull_request"], "execute")
        self.assertEqual(operations["list_story_pull_requests"], "read")
        self.assertEqual(operations["get_story_cherry_pick"], "read")
        for name in operations:
            if name in {"publish_story_pull_request", "cherry_pick_story_pull_request",
                        "list_story_pull_requests", "get_story_cherry_pick"}:
                self.assertEqual(CAPABILITIES[name][0], operations[name])


class ListTests(ServiceTestCase):
    def test_lists_only_matching_story_pull_requests(self) -> None:
        relations = [
            SimpleNamespace(rel="ArtifactLink", url="vstfs:///Git/PullRequestId/proj%2frepo-1%2f77",
                            attributes={"name": "Pull Request"}),
            SimpleNamespace(rel="ArtifactLink", url="vstfs:///Git/PullRequestId/proj%2frepo-1%2f78",
                            attributes={"name": "Pull Request"}),
            SimpleNamespace(rel="ArtifactLink", url="vstfs:///Git/Ref/proj%2frepo-1%2fGBbranch",
                            attributes={"name": "Branch"}),
        ]
        self.git.get_pull_request_by_id.side_effect = lambda identifier: make_pull_request(
            pull_request_id=identifier, status="active" if identifier == 77 else "completed")
        with patch.object(services, "_get_work_item_snapshot", return_value={"relations": relations}):
            result = json.loads(services._list_story_pull_requests(1, target_branch="stage-pre-prod"))
        self.assertEqual(result["count"], 1)
        pull_request = result["pull_requests"][0]
        self.assertEqual(pull_request["pull_request_id"], 77)
        self.assertEqual(pull_request["source_commit_id"], SOURCE_COMMIT)
        self.assertTrue(pull_request["is_draft"])


class PublishTests(ServiceTestCase):
    def test_publish_updates_draft_and_confirms(self) -> None:
        self.git.get_pull_request_by_id.side_effect = [make_pull_request(), make_pull_request(draft=False)]
        result = json.loads(services._publish_story_pull_request("accountant_backend", 77, SOURCE_COMMIT))
        self.assertTrue(result["published"])
        update = self.git.update_pull_request.call_args
        self.assertIs(update.args[0].is_draft, False)
        self.assertEqual(update.kwargs["pull_request_id"], 77)

    def test_publish_rejects_unsafe_states_without_writing(self) -> None:
        cases = {
            "published": make_pull_request(draft=False),
            "completed": make_pull_request(status="completed"),
            "changed": make_pull_request(commit="b" * 40),
            "other_repo": make_pull_request(repository_id="repo-2"),
        }
        for name, pull_request in cases.items():
            with self.subTest(case=name):
                self.git.get_pull_request_by_id.side_effect = [pull_request]
                with self.assertRaises(ValueError):
                    services._publish_story_pull_request("accountant_backend", 77, SOURCE_COMMIT)
                self.git.update_pull_request.assert_not_called()

    def test_publish_fails_when_azure_does_not_confirm(self) -> None:
        self.git.get_pull_request_by_id.side_effect = [make_pull_request(), make_pull_request()]
        with self.assertRaises(RuntimeError):
            services._publish_story_pull_request("accountant_backend", 77, SOURCE_COMMIT)


class CherryPickTests(ServiceTestCase):
    GENERATED = "hotfix/KL-1-x-on-stage-homolog"

    def setUp(self) -> None:
        super().setUp()
        self.git.get_pull_request_by_id.return_value = make_pull_request()
        existing = {"stage-homolog"}
        self.git.get_refs.side_effect = lambda **kwargs: [
            SimpleNamespace(name=f"refs/heads/{name}") for name in existing
            if kwargs["filter"] == f"heads/{name}"]
        sleeper = patch.object(services.time, "sleep", lambda seconds: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def run_cherry_pick(self, **overrides: object) -> dict[str, object]:
        arguments = {"repository_name": "accountant_backend", "pull_request_id": 77,
                     "target_branch": "stage-homolog", "generated_branch": self.GENERATED,
                     "expected_source_commit": SOURCE_COMMIT, **overrides}
        return json.loads(services._cherry_pick_story_pull_request(**arguments))

    def test_waits_until_completed(self) -> None:
        self.git.create_cherry_pick.return_value = make_cherry_pick("queued")
        self.git.get_cherry_pick.side_effect = [make_cherry_pick("inProgress"), make_cherry_pick("completed")]
        result = self.run_cherry_pick()
        self.assertTrue(result["completed"])
        self.assertFalse(result["conflict"])
        parameters = self.git.create_cherry_pick.call_args.args[0]
        self.assertEqual(parameters.onto_ref_name, "refs/heads/stage-homolog")
        self.assertEqual(parameters.generated_ref_name, f"refs/heads/{self.GENERATED}")
        self.assertEqual(parameters.source.pull_request_id, 77)

    def test_reports_conflict(self) -> None:
        self.git.create_cherry_pick.return_value = make_cherry_pick("failed", conflict=True, message="conflito")
        result = self.run_cherry_pick()
        self.assertFalse(result["completed"])
        self.assertTrue(result["conflict"])
        self.assertEqual(result["failure_message"], "conflito")

    def test_rejected_request_without_branch_is_a_confirmed_failure(self) -> None:
        self.git.create_cherry_pick.side_effect = RuntimeError("requires user authentication")
        result = self.run_cherry_pick()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["completed"])
        self.assertIn("requires user authentication", result["failure_message"])

    def test_error_with_branch_present_is_not_swallowed(self) -> None:
        self.git.create_cherry_pick.side_effect = RuntimeError("boom")
        calls = {"n": 0}

        def refs(**kwargs: object) -> list[SimpleNamespace]:
            calls["n"] += 1
            name = str(kwargs["filter"])[6:]
            if name == "stage-homolog" or calls["n"] > 2:
                return [SimpleNamespace(name=f"refs/heads/{name}")]
            return []

        self.git.get_refs.side_effect = refs
        with self.assertRaises(RuntimeError):
            self.run_cherry_pick()

    def test_accepts_legacy_cherry_pick_name(self) -> None:
        self.git.create_cherry_pick.return_value = make_cherry_pick("completed")
        result = self.run_cherry_pick(generated_branch="cherry-pick/KL-1-accountant_backend-stage-homolog-pr77")
        self.assertTrue(result["completed"])

    def test_returns_in_progress_when_wait_expires(self) -> None:
        self.git.create_cherry_pick.return_value = make_cherry_pick("inProgress")
        result = self.run_cherry_pick(wait_seconds=0)
        self.assertEqual(result["status"], "inProgress")
        self.git.get_cherry_pick.assert_not_called()

    def test_rejects_unsafe_requests_without_creating(self) -> None:
        cases = {
            "bad_name": {"generated_branch": "feature/qualquer"},
            "name_not_from_source": {"generated_branch": "hotfix/KL-1-outra-on-stage-homolog"},
            "missing_target": {"target_branch": "inexistente"},
            "same_as_pr_target": {"target_branch": "stage-pre-prod"},
            "stale_commit": {"expected_source_commit": "b" * 40},
        }
        for name, overrides in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                self.run_cherry_pick(**overrides)
            self.git.create_cherry_pick.assert_not_called()

    def test_rejects_existing_generated_branch(self) -> None:
        self.git.get_refs.side_effect = lambda **kwargs: [SimpleNamespace(name=f"refs/heads/{kwargs['filter'][6:]}")]
        with self.assertRaises(ValueError):
            self.run_cherry_pick()
        self.git.create_cherry_pick.assert_not_called()

    def test_status_reader(self) -> None:
        self.git.get_cherry_pick.return_value = make_cherry_pick("completed")
        result = json.loads(services._get_story_cherry_pick("accountant_backend", 9))
        self.assertTrue(result["completed"])


if __name__ == "__main__":
    unittest.main()
