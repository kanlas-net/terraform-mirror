"""Packaging checks: Dockerfile, compose, pyproject and the README stay in sync."""

from __future__ import annotations

import importlib.util
import os
import re
import unittest

import terraform_mirror

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name: str) -> str:
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as handle:
        return handle.read()


def github_glob_to_regex(pattern: str) -> str:
    """Translate a GitHub Actions filter pattern into a regex.

    Mirrors the "Filter pattern cheat sheet" in the workflow syntax reference:
    ``**`` spans ``/``, ``*`` stops at ``/``, ``?`` and ``+`` quantify the
    preceding character and ``[...]`` is an alphanumeric class. Everything else
    is literal -- these are globs, not regular expressions.
    """
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        elif pattern[index] == "+":
            out.append("+")
            index += 1
        elif pattern[index] == "[":
            end = pattern.find("]", index + 1)
            if end < 0:
                out.append(re.escape(pattern[index]))
                index += 1
            else:
                out.append(pattern[index : end + 1])
                index = end + 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return "".join(out)


def env_names_in_code() -> set[str]:
    return set(re.findall(r'"(TFM_[A-Z0-9_]+)"', read("terraform_mirror/config.py")))


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        self.text = read("Dockerfile")

    def test_base_image_and_layout(self):
        self.assertIn("FROM python:3.12-slim AS runtime", self.text)
        self.assertIn("COPY terraform_mirror/ /app/terraform_mirror/", self.text)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/python", "-m", "terraform_mirror", "serve"]', self.text)

    def test_runs_unprivileged_on_the_default_port(self):
        self.assertIn("EXPOSE 8080", self.text)
        user_lines = [line for line in self.text.splitlines() if line.startswith("USER ")]
        self.assertTrue(user_lines, "the container must not run as root")
        self.assertNotIn("USER root", self.text.split("USER terraform_mirror")[-1])

    def test_healthcheck_uses_the_env_aware_probe(self):
        """The probe must follow the environment, not a literal URL.

        ``http://127.0.0.1:8080/healthz`` was unhealthy forever with ``TFM_TLS_*`` set —
        the same socket comes back TLS-wrapped — and wrong for any ``TFM_PORT`` other than
        8080. ``terraform-mirror ping`` reads ``TFM_HOST``/``TFM_PORT``/``TFM_TLS_CERT`` instead.
        """
        self.assertIn("HEALTHCHECK", self.text)
        probe = self.text.split("HEALTHCHECK", 1)[1]
        self.assertIn('"-m", "terraform_mirror", "ping"', probe)
        self.assertNotIn("127.0.0.1", probe, "the probe must not hardcode the address")
        self.assertNotIn("urlopen", probe, "the probe must not hardcode the scheme")

    def test_test_stage_runs_the_suite(self):
        self.assertIn("FROM python:3.12-slim AS test", self.text)
        self.assertIn("unittest", self.text)

    def test_dockerignore_excludes_the_host_cache(self):
        self.assertIn("cache/", read(".dockerignore"))

    def test_dockerignore_keeps_everything_the_suite_needs(self):
        """The ``test`` stage does ``COPY .`` and runs the suite, so its inputs must be
        in the build context: excluding ``tests/`` or ``*.md`` breaks ``--target test``.
        """
        patterns = {
            line.strip()
            for line in read(".dockerignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        needed = ("LICENSE", "README.md", "CHANGELOG.md", "AGENTS.md", "pyproject.toml", "docker-compose.yml", "examples", "tests", "terraform_mirror")
        for name in needed:
            with self.subTest(path=name):
                self.assertNotIn(name, patterns, f"{name} must stay in the build context")
                self.assertNotIn(name + "/", patterns, f"{name}/ must stay in the build context")
        self.assertNotIn("*.md", patterns, "*.md hides README.md and CHANGELOG.md")


class ComposeTest(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_compose_is_valid_and_uses_known_settings(self):
        import yaml

        document = yaml.safe_load(read("docker-compose.yml"))
        self.assertIn("services", document)
        services = document["services"]
        self.assertIn("terraform-mirror-a", services)
        self.assertIn("terraform-mirror-b", services)
        env = services["terraform-mirror-a"]["environment"]
        self.assertIn("TFM_S3_BUCKET", env)
        self.assertIn("TFM_LOCK_TTL", env)
        known = env_names_in_code()
        unknown = {key for key in env if key.startswith("TFM_") and key not in known}
        self.assertEqual(unknown, set(), f"compose sets unknown settings: {unknown}")
        self.assertIn("volumes", document)

    def test_two_services_share_one_bucket(self):
        text = read("docker-compose.yml")
        self.assertIn("extends: terraform-mirror-a", text, "the second instance must inherit the same bucket")
        self.assertEqual(text.count("TFM_S3_BUCKET:"), 1)


class ProjectMetadataTest(unittest.TestCase):
    def test_pyproject_version_matches_the_package(self):
        text = read("pyproject.toml")
        self.assertIn(f'version = "{terraform_mirror.__version__}"', text)

    def test_no_runtime_dependencies(self):
        self.assertIn("dependencies = []", read("pyproject.toml"))

    def test_console_script_is_declared(self):
        self.assertIn('terraform-mirror = "terraform_mirror.__main__:main"', read("pyproject.toml"))

    def test_no_third_party_imports_in_the_package(self):
        import sys as _sys

        allowed = set(getattr(_sys, "stdlib_module_names", set())) | {"terraform_mirror"}
        offenders = set()
        for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, "terraform_mirror")):
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, "r", encoding="utf-8") as handle:
                    source = handle.read()
                for match in re.finditer(r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_]*)", source, re.M):
                    module = match.group(1)
                    if module not in allowed and not module.startswith("terraform_mirror"):
                        offenders.add(f"{os.path.relpath(path, ROOT)}: {module}")
        self.assertEqual(offenders, set(), "the runtime must stay standard-library only")


class ReleaseEngineeringTest(unittest.TestCase):
    """The GitHub workflows that version and publish the image stay honest."""

    def workflow(self, name: str) -> str:
        path = os.path.join(ROOT, ".github", "workflows", name)
        if not os.path.exists(path):
            # ``--target test`` builds from a context without .github (see .dockerignore).
            self.skipTest(f"{name} is not in the build context")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_version_is_a_plain_semver(self):
        self.assertRegex(terraform_mirror.__version__, r"^\d+\.\d+\.\d+$")

    def test_license_is_shipped(self):
        self.assertIn("MIT License", read("LICENSE"))

    def test_changelog_has_an_entry_for_the_current_version(self):
        self.assertIn(f"## [{terraform_mirror.__version__}]", read("CHANGELOG.md"))

    def test_ci_builds_and_smoke_tests_the_image(self):
        text = self.workflow("ci.yml")
        self.assertIn("unittest discover", text)
        self.assertIn("docker/build-push-action", text)
        self.assertIn("/healthz", text)
        self.assertNotIn("packages: write", text, "CI must not publish anything")

    def test_ci_waits_for_the_builtin_healthcheck(self):
        """The image's own ``HEALTHCHECK`` is what an operator sees as "unhealthy".

        It has to be proven on both schemes: a plain http probe against a TLS socket never
        goes healthy, which is exactly how the hardcoded probe used to fail in production.
        """
        text = self.workflow("ci.yml")
        self.assertIn("State.Health.Status", text, "CI must wait for the image HEALTHCHECK, not only curl")
        self.assertIn("TFM_TLS_CERT", text, "the HEALTHCHECK must be proven over TLS too")

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_release_publishes_to_ghcr_under_a_version_tag(self):
        import yaml

        document = yaml.safe_load(self.workflow("release.yml"))
        self.assertEqual(document["permissions"]["packages"], "write")
        text = self.workflow("release.yml")
        self.assertIn("ghcr.io", text)
        self.assertIn("terraform_mirror.__version__", text, "the tag must be checked against the package version")
        self.assertIn("type=semver,pattern={{version}}", text)
        self.assertIn("type=raw,value=latest", text)

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_jobs_that_shell_out_to_git_or_gh_check_the_tree_out(self):
        """``gh`` and ``git`` need a working tree, and a job starts without one.

        The release job failed with "failed to run git: fatal: not a git
        repository" because it only called gh and never checked anything out.
        """
        import yaml

        for name in ("ci.yml", "release.yml"):
            document = yaml.safe_load(self.workflow(name))
            for job_id, job in document["jobs"].items():
                steps = job.get("steps", [])
                script = "\n".join(str(step.get("run", "")) for step in steps)
                if not re.search(r"\b(gh|git)\s", script):
                    continue
                self.assertTrue(
                    any(str(step.get("uses", "")).startswith("actions/checkout") for step in steps),
                    f"{name}:{job_id} runs gh/git without actions/checkout",
                )

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_a_leftover_draft_release_is_published_not_skipped(self):
        """Deleting a tag turns its release into a draft, it does not delete it.

        Re-tagging a version left v1.0.0 as an unpublished draft: the step only asked
        whether the release existed, ``gh release view`` answers yes for a draft, and the
        step printed "already exists" while the release page stayed empty.
        """
        import yaml

        document = yaml.safe_load(self.workflow("release.yml"))
        steps = document["jobs"]["release-notes"]["steps"]
        script = "\n".join(str(step.get("run", "")) for step in steps)
        self.assertIn("isDraft", script, "the step must look at whether the release is a draft")
        self.assertRegex(script, r"gh\s+release\s+edit[^\n]*--draft=false", "a draft must be published")
        # The publish has to happen on the "release exists" path, not instead of it.
        self.assertLess(script.index("gh release view"), script.index("--draft=false"))
        self.assertIn("gh release create", script, "a release that does not exist yet must be created")

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_release_tag_filter_actually_matches_the_version_tag(self):
        """A tag filter that never matches silently publishes nothing.

        The first version of this workflow used ``v[0-9]+.[0-9]+.[0-9]+*`` and
        pushed v1.0.0 without a single release run appearing, so match the
        patterns instead of comparing them as strings.
        """
        import yaml

        document = yaml.safe_load(self.workflow("release.yml"))
        # PyYAML resolves the bare key ``on`` as the boolean True.
        triggers = document["on"] if "on" in document else document[True]
        patterns = triggers["push"]["tags"]
        self.assertTrue(patterns, "release.yml must trigger on a tag push")

        def matches(ref: str) -> bool:
            return any(re.fullmatch(github_glob_to_regex(p), ref) for p in patterns)

        self.assertTrue(matches(f"v{terraform_mirror.__version__}"), "the tag we tag releases with must trigger")
        self.assertTrue(matches("v1.0.0"))
        self.assertTrue(matches("v10.20.30"))
        self.assertFalse(matches("v1"), "a bare major is not a release")
        self.assertFalse(matches("v1.2"), "a bare major.minor is not a release")
        self.assertFalse(matches("latest"))
        self.assertFalse(matches("release-v1.0.0"))

    def test_glob_translator_follows_the_github_cheat_sheet(self):
        def matches(pattern: str, ref: str) -> bool:
            return re.fullmatch(github_glob_to_regex(pattern), ref) is not None

        self.assertTrue(matches("v*.*.*", "v1.0.0"))
        self.assertTrue(matches("feature/*", "feature/my-branch"))
        self.assertFalse(matches("feature/*", "feature/a/b"), "* must not cross a slash")
        self.assertTrue(matches("feature/**", "feature/a/b"))
        self.assertTrue(matches("v[0-9]*", "v12"))
        self.assertFalse(matches("v[0-9]*", "vx"))


class RepositoryLanguageTest(unittest.TestCase):
    """The project is public and documented in one language."""

    SUFFIXES = (".py", ".md", ".yml", ".yaml", ".toml", ".tf", ".txt", ".cfg", ".ini")

    def test_no_cyrillic_anywhere_in_the_repository(self):
        offenders = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".venv", "cache", ".tf-cache"}]
            for filename in filenames:
                if not filename.endswith(self.SUFFIXES):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    if re.search(r"[\u0400-\u04FF]", handle.read()):
                        offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [], f"cyrillic text found in: {offenders}")


class AgentsGuideTest(unittest.TestCase):
    """AGENTS.md is the entry point for coding agents: stale instructions are worse than none."""

    def test_the_guide_exists_and_states_the_test_command(self):
        text = read("AGENTS.md")
        self.assertIn("unittest discover", text, "the guide must say how to run the suite")
        self.assertIn("standard library", text.lower(), "the stdlib-only rule is the first thing an agent breaks")

    def test_every_test_the_guide_cites_can_be_loaded(self):
        """A citation to a test that does not exist sends an agent hunting for a phantom."""
        import importlib

        text = read("AGENTS.md")

        dotted = set(re.findall(r"\btests\.[a-z_][a-z_0-9]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}", text))
        self.assertTrue(dotted, "the guide should point at concrete tests")
        loader = unittest.TestLoader()
        for citation in sorted(dotted):
            with self.subTest(citation=citation):
                loader.errors = []
                suite = loader.loadTestsFromName(citation)
                self.assertFalse(loader.errors, f"{citation} does not import: {loader.errors}")
                self.assertGreater(suite.countTestCases(), 0, f"{citation} resolves to no tests")

        test_dir = os.path.join(ROOT, "tests")
        test_modules = sorted(
            f"tests.{filename[:-3]}"
            for filename in os.listdir(test_dir)
            if filename.startswith("test_") and filename.endswith(".py")
        )
        for cls, method in sorted(set(re.findall(r"`([A-Z]\w+Test)\.([a-z_][a-z_0-9]+)`", text))):
            with self.subTest(citation=f"{cls}.{method}"):
                hits = [
                    module
                    for module in (importlib.import_module(name) for name in sorted(test_modules))
                    if hasattr(module, cls) and hasattr(getattr(module, cls), method)
                ]
                self.assertTrue(hits, f"{cls}.{method} is not defined by any test module")


class ReadmeTest(unittest.TestCase):
    def test_every_setting_is_documented(self):
        readme = read("README.md")
        missing = sorted(name for name in env_names_in_code() if name not in readme)
        self.assertEqual(missing, [], f"undocumented settings: {missing}")

    def test_client_config_example_exists(self):
        example = read("examples/terraform-cli-config.tf")
        self.assertIn("modules.v1", example)
        self.assertIn("registry.terraform.io", example)

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_kubernetes_example_probes_the_right_endpoints(self):
        """A readiness probe aimed at ``/healthz`` silently defeats the whole point.

        Liveness must stay local: if it probes the bucket, an S3 outage restarts every pod
        instead of just draining them.
        """
        import yaml

        documents = [doc for doc in yaml.safe_load_all(read("examples/kubernetes.yaml")) if doc]
        deployment = next(doc for doc in documents if doc.get("kind") == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        paths = {
            name: container[name]["httpGet"]["path"]
            for name in ("startupProbe", "readinessProbe", "livenessProbe")
        }
        self.assertEqual(paths, {"startupProbe": "/readyz", "readinessProbe": "/readyz", "livenessProbe": "/healthz"})
        names = {port["name"] for port in container["ports"]}
        for name in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertIn(container[name]["httpGet"]["port"], names)
        service = next(doc for doc in documents if doc.get("kind") == "Service")
        self.assertEqual(service["spec"]["selector"], deployment["spec"]["selector"]["matchLabels"])

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_kubernetes_example_outlives_its_own_shutdown_grace(self):
        """If kubelet sends SIGKILL first, the locks survive and only the TTL cleans them up."""
        import yaml

        documents = [doc for doc in yaml.safe_load_all(read("examples/kubernetes.yaml")) if doc]
        deployment = next(doc for doc in documents if doc.get("kind") == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        config_map = next(doc for doc in documents if doc.get("kind") == "ConfigMap")
        grace = int(pod["terminationGracePeriodSeconds"])
        shutdown_grace = int(config_map["data"]["TFM_SHUTDOWN_GRACE"])
        self.assertGreater(
            grace, shutdown_grace, "terminationGracePeriodSeconds must be greater than TFM_SHUTDOWN_GRACE"
        )

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_alert_rules_only_use_exported_metrics(self):
        """An alert on a metric the proxy never writes is discovered during the incident."""
        import yaml

        from terraform_mirror.metrics import Metrics

        document = yaml.safe_load(read("examples/prometheus-alerts.yaml"))
        rules = [rule for group in document["spec"]["groups"] for rule in group["rules"]]
        self.assertTrue(rules, "the example must actually carry rules")
        registry = Metrics().registry
        exported: set[str] = set()
        for name in registry.names():
            exported.add(name)
            metric = registry.metric(name)
            if metric is not None and metric.kind == "histogram":
                exported.update({f"{name}_bucket", f"{name}_count", f"{name}_sum"})
        used = set(re.findall(r"terraform_mirror_[a-z_]+", read("examples/prometheus-alerts.yaml")))
        self.assertTrue(used)
        unknown = sorted(name for name in used if name not in exported)
        self.assertEqual(unknown, [], f"alerts use metrics that are never exported: {unknown}")

    def test_documented_endpoints_are_routed(self):
        from terraform_mirror.server import _ROUTES

        routes = {pattern.pattern for _verb, pattern, _name in _ROUTES}
        readme = read("README.md")
        for placeholder, needle in (
            ("/v1/modules/{ns}/{name}/{prov}/versions", "v1/modules"),
            ("/archive/{ns}/{name}/{prov}/{ver}.zip", "archive"),
            ("/.well-known/terraform.json", "well-known"),
            ("/api/v1/prefetch", "prefetch"),
            ("/api/v1/pending", "pending"),
            ("/healthz", "healthz"),
            ("/readyz", "readyz"),
            ("/metrics", "metrics"),
        ):
            with self.subTest(route=placeholder):
                self.assertIn(needle, readme)
                self.assertTrue(any(needle.split("/")[0] in route for route in routes), routes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
