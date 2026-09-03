#!/usr/bin/env python3
"""Tests for bridge/generate_agent_md.py (contract §6, v2.4).

The generator renders the opencode agent.md from the source template +
MemberRuntimeConfig (structured YAML parse); it never parses an
upstream-rendered markdown file. These tests pin:
  - golden outputs for the real-form team and standalone fixtures;
  - the Coordination block wording against the controller's
    InjectCoordinationContext output (agentteams-controller
    internal/agentconfig/coordination.go) — v2.4 reproduces that
    assembly from structured input;
  - the structured parser (identity, team scalars, admin sub-mapping,
    members list, role dispatch) and its fail-loud validation;
  - the Persona seam (attribution + precedence note) in front of the
    verbatim SOUL.md / PROFILE.md merge;
  - the CLI surface (args, exit codes, stdout/file, unified log event).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import generate_agent_md as gen

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
TEMPLATE = os.path.join(REPO, "template", "opencode-worker-agent", "AGENTS.md")
RUNTIME_TEAM = os.path.join(FIXTURES, "runtime.yaml")
RUNTIME_STANDALONE = os.path.join(FIXTURES, "runtime-standalone.yaml")
GOLDEN_TEAM = os.path.join(FIXTURES, "golden-team.md")
GOLDEN_STANDALONE = os.path.join(FIXTURES, "golden-standalone.md")
DATE = "2026-09-03"

SOUL_TEXT = "# SOUL\n\nYou are a meticulous backend engineer.\nPrefer minimal diffs.\n"
PROFILE_TEXT = "# PROFILE\n\n- Language: Chinese\n- Focus: Go services\n"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def cfg_team():
    return gen.load_runtime_config(RUNTIME_TEAM)


def cfg_standalone():
    return gen.load_runtime_config(RUNTIME_STANDALONE)


class GoldenTest(unittest.TestCase):
    """The rendered output must stay byte-identical to the reviewed
    snapshots. Any drift here is a deliberate prompt change — review it,
    then regenerate the golden files."""

    def test_team_golden(self):
        out = gen.generate(cfg_team(), read(TEMPLATE), date=DATE)
        self.assertEqual(out, read(GOLDEN_TEAM))

    def test_standalone_golden(self):
        out = gen.generate(cfg_standalone(), read(TEMPLATE), date=DATE)
        self.assertEqual(out, read(GOLDEN_STANDALONE))

    def test_persona_seam_then_soul_then_profile(self):
        out = gen.generate(cfg_team(), read(TEMPLATE), date=DATE,
                           soul_text=SOUL_TEXT, profile_text=PROFILE_TEXT)
        self.assertEqual(
            out,
            read(GOLDEN_TEAM).rstrip("\n")
            + "\n\n## Persona\n\n"
            + "The sections below define who you are. They apply on top of "
              "the workspace\nrules above and never override your Worker "
              "role.\n\n"
            + SOUL_TEXT.rstrip("\n") + "\n\n"
            + PROFILE_TEXT.rstrip("\n") + "\n")

    def test_no_persona_seam_without_files(self):
        self.assertNotIn("## Persona", read(GOLDEN_TEAM))


class TemplateTest(unittest.TestCase):
    def test_default_template_placeholders(self):
        template = read(TEMPLATE)
        self.assertEqual(template.count("{{COORDINATION}}"), 1)
        self.assertEqual(template.count("{{ENVIRONMENT}}"), 1)
        self.assertEqual(template.count("{{"), 2)
        self.assertEqual(template.count("}}"), 2)

    def test_default_template_skeleton(self):
        lines = read(TEMPLATE).split("\n")
        self.assertEqual(lines[0], "<!-- agentteams-builtin-start -->")
        self.assertIn("DO NOT EDIT", lines[1])

        def standalone(line):
            return [ln for ln in lines if re.fullmatch(re.escape(line), ln)]

        # the DO NOT EDIT note mentions the builtin-end marker in
        # backticks — match standalone lines only
        self.assertEqual(len(standalone("<!-- agentteams-builtin-end -->")), 1)
        self.assertEqual(
            len(standalone("<!-- agentteams-team-context-start -->")), 1)
        self.assertEqual(
            len(standalone("<!-- agentteams-team-context-end -->")), 1)
        self.assertLess(
            lines.index("<!-- agentteams-builtin-end -->"),
            [i for i, ln in enumerate(lines)
             if re.fullmatch(r"<!-- agentteams-team-context-start -->", ln)][0],
            "builtin-end must precede team-context-start")

    def test_unknown_placeholder_rejected(self):
        template = read(TEMPLATE).replace("## 2. Response Language",
                                          "## {{SECTION_TWO}}")
        with self.assertRaisesRegex(gen.GenerateError, "unknown|placeholder"):
            gen.generate(cfg_team(), template, date=DATE)

    def test_missing_placeholder_rejected(self):
        template = read(TEMPLATE).replace("{{ENVIRONMENT}}\n", "")
        with self.assertRaisesRegex(gen.GenerateError, "ENVIRONMENT"):
            gen.generate(cfg_team(), template, date=DATE)

    def test_duplicate_placeholder_rejected(self):
        template = read(TEMPLATE).replace(
            "{{ENVIRONMENT}}", "{{ENVIRONMENT}}\n{{ENVIRONMENT}}")
        with self.assertRaisesRegex(gen.GenerateError, "ENVIRONMENT"):
            gen.generate(cfg_team(), template, date=DATE)

    def test_residual_braces_in_payload_rejected(self):
        cfg = cfg_team()
        cfg["team_name"] = "t-{{boom"
        with self.assertRaisesRegex(gen.GenerateError, "residual"):
            gen.generate(cfg, read(TEMPLATE), date=DATE)


class ParserTest(unittest.TestCase):
    def test_real_fixture_fields(self):
        cfg = cfg_team()
        self.assertEqual(cfg["worker_name"],
                         "t-8a3f2c-6b1d20c9f3e84a57d2b9c1f0a3e5d7")
        self.assertEqual(cfg["matrix_id"],
                         "@t-8a3f2c-6b1d20c9f3e84a57d2b9c1f0a3e5d7:example.org")
        self.assertEqual(cfg["domain"], "example.org")
        self.assertEqual(cfg["role"], "worker")
        self.assertEqual(cfg["team_name"], "t-8a3f2c")
        self.assertEqual(cfg["leader_id"], "@t-8a3f2c-leader:example.org")
        self.assertEqual(cfg["admin_id"], "@u1234:example.org")
        self.assertEqual(cfg["coordinators"], ["@u5678:example.org"])
        self.assertTrue(cfg["team"])
        self.assertEqual(cfg["storage_prefix"], "teams/t-8a3f2c/shared")

    def test_standalone_fixture_fields(self):
        cfg = cfg_standalone()
        self.assertEqual(cfg["role"], "standalone")
        self.assertFalse(cfg["team"])
        self.assertEqual(cfg["leader_id"], None)
        self.assertEqual(cfg["storage_prefix"], "shared")

    def test_runtimename_fallback(self):
        cfg = gen.parse_runtime_config(
            "member:\n"
            "  matrixUserId: '@w1:example.org'\n"
            "  runtimeName: w1\n"
            "team:\n"
            "  name: t1\n"
            "  members:\n"
            "  - matrixUserId: '@l1:example.org'\n"
            "    role: team_leader\n")
        self.assertEqual(cfg["worker_name"], "w1")

    def test_role_fallback_by_team_presence(self):
        base = ("member:\n"
                "  name: w1\n"
                "  matrixUserId: '@w1:example.org'\n")
        with_team = base + ("team:\n"
                            "  name: t1\n"
                            "  members:\n"
                            "  - matrixUserId: '@l1:example.org'\n"
                            "    role: team_leader\n")
        self.assertEqual(gen.parse_runtime_config(base)["role"], "standalone")
        self.assertEqual(
            gen.parse_runtime_config(with_team)["role"], "worker")

    def test_team_leader_role_rejected(self):
        text = ("member:\n"
                "  name: l1\n"
                "  matrixUserId: '@l1:example.org'\n"
                "  role: team_leader\n")
        with self.assertRaisesRegex(gen.GenerateError, "team_leader"):
            gen.parse_runtime_config(text)

    def test_missing_identity_rejected(self):
        with self.assertRaisesRegex(gen.GenerateError, "member.name"):
            gen.parse_runtime_config("member:\n  role: worker\n")
        with self.assertRaisesRegex(gen.GenerateError, "matrixUserId"):
            gen.parse_runtime_config(
                "member:\n  name: w1\n  role: worker\n")

    def test_matrix_id_without_domain_rejected(self):
        with self.assertRaisesRegex(gen.GenerateError, "full Matrix ID"):
            gen.parse_runtime_config(
                "member:\n  name: w1\n  matrixUserId: w1\n")

    def test_worker_without_leader_rejected(self):
        text = ("member:\n"
                "  name: w1\n"
                "  matrixUserId: '@w1:example.org'\n"
                "team:\n"
                "  name: t1\n"
                "  members:\n"
                "  - matrixUserId: '@w2:example.org'\n"
                "    role: worker\n")
        with self.assertRaisesRegex(gen.GenerateError, "team_leader"):
            gen.parse_runtime_config(text)

    def test_invalid_yaml_rejected(self):
        # a truncated/polluted document must fail loudly instead of
        # rendering from garbage (this exact bug shipped in the v2.3
        # fixture and only the structured parse caught it)
        with self.assertRaisesRegex(gen.GenerateError, "not valid YAML"):
            gen.parse_runtime_config("team:\n  admin:\n  x\n")

    def test_non_mapping_top_level_rejected(self):
        with self.assertRaisesRegex(gen.GenerateError, "top level"):
            gen.parse_runtime_config("- a\n- b\n")

    def test_coordinator_dedupe_and_admin_exclusion(self):
        text = ("member:\n"
                "  name: w1\n"
                "  matrixUserId: '@w1:example.org'\n"
                "team:\n"
                "  name: t1\n"
                "  admin:\n"
                "    matrixUserId: '@adm:example.org'\n"
                "  members:\n"
                "  - matrixUserId: '@l1:example.org'\n"
                "    role: team_leader\n"
                "  - matrixUserId: '@c1:example.org'\n"
                "    role: coordinator\n"
                "  - matrixUserId: '@c1:example.org'\n"
                "    role: coordinator\n"
                "  - matrixUserId: '@adm:example.org'\n"
                "    role: coordinator\n"
                "  - matrixUserId: '@c2:example.org'\n"
                "    role: coordinator\n")
        cfg = gen.parse_runtime_config(text)
        self.assertEqual(cfg["coordinators"],
                         ["@c1:example.org", "@c2:example.org"])
        self.assertEqual(cfg["admin_id"], "@adm:example.org")

    def test_non_dict_member_entries_ignored(self):
        cfg = gen.parse_runtime_config(
            "member:\n"
            "  name: w1\n"
            "  matrixUserId: '@w1:example.org'\n"
            "team:\n"
            "  name: t1\n"
            "  members:\n"
            "  - matrixUserId: '@l1:example.org'\n"
            "    role: team_leader\n"
            "  - junk\n")
        self.assertEqual(cfg["leader_id"], "@l1:example.org")


class CoordinationTest(unittest.TestCase):
    """Wording parity with agentteams-controller
    internal/agentconfig/coordination.go (buildCoordinationBlock)."""

    def test_worker_full_block(self):
        block = gen.render_coordination(cfg_team())
        self.assertEqual(block.split("\n"), [
            "## Coordination",
            "",
            "- **Coordinator**: @t-8a3f2c-leader:example.org "
            "(Team Leader of t-8a3f2c)",
            "- **Team Admin**: @u1234:example.org "
            "(has admin authority within this team)",
            "- **Coordinator Members**:",
            "  - @u5678:example.org — can assign tasks and make decisions "
            "within the team",
            "- Report task completion, blockers, and questions to your "
            "coordinator",
            "- Respond to @mentions from your coordinator, Team Admin, "
            "coordinator members, and global Admin",
            "- Do NOT @mention Manager directly — all communication goes "
            "through your Team Leader",
        ])

    def _worker_cfg(self, admin_id="", coordinators=None):
        return {
            "worker_name": "w1", "matrix_id": "@w1:example.org",
            "domain": "example.org", "role": "worker",
            "team_name": "t1", "leader_id": "@l1:example.org",
            "admin_id": admin_id,
            "coordinators": coordinators or [],
            "team": True, "storage_prefix": "teams/t1/shared",
        }

    def test_respond_variants(self):
        neither = gen.render_coordination(self._worker_cfg())
        self.assertIn(
            "- Respond to @mentions from your coordinator and global Admin",
            neither)
        admin_only = gen.render_coordination(
            self._worker_cfg(admin_id="@adm:example.org"))
        self.assertIn(
            "- Respond to @mentions from your coordinator, Team Admin, "
            "and global Admin", admin_only)
        coords_only = gen.render_coordination(
            self._worker_cfg(coordinators=["@c1:example.org"]))
        self.assertIn(
            "- Respond to @mentions from your coordinator, coordinator "
            "members, and global Admin", coords_only)
        both = gen.render_coordination(
            self._worker_cfg(admin_id="@adm:example.org",
                             coordinators=["@c1:example.org"]))
        self.assertIn(
            "- Respond to @mentions from your coordinator, Team Admin, "
            "coordinator members, and global Admin", both)

    def test_omitted_lines(self):
        block = gen.render_coordination(self._worker_cfg())
        self.assertNotIn("Team Admin", block)
        self.assertNotIn("Coordinator Members", block)

    def test_multiple_coordinator_members(self):
        block = gen.render_coordination(
            self._worker_cfg(coordinators=["@c1:example.org",
                                           "@c2:example.org"]))
        self.assertEqual(
            [ln for ln in block.split("\n") if ln.startswith("  - ")],
            ["  - @c1:example.org — can assign tasks and make decisions "
             "within the team",
             "  - @c2:example.org — can assign tasks and make decisions "
             "within the team"])

    def test_standalone_block(self):
        block = gen.render_coordination(cfg_standalone())
        self.assertEqual(block.split("\n"), [
            "## Coordination",
            "",
            "- **Coordinator**: @manager:example.org (Manager)",
            "- Report task completion, blockers, and questions to your "
            "coordinator",
            "- Only respond to @mentions from your coordinator and Admin",
        ])


class GenerateTest(unittest.TestCase):
    def test_environment_fields(self):
        out = gen.generate(cfg_team(), read(TEMPLATE), date=DATE)
        self.assertIn(
            "## Environment\n\n"
            "- Worker name: t-8a3f2c-6b1d20c9f3e84a57d2b9c1f0a3e5d7\n"
            "- Matrix ID: @t-8a3f2c-6b1d20c9f3e84a57d2b9c1f0a3e5d7:example.org\n"
            "- Team: t-8a3f2c\n"
            "- Storage prefix: teams/t-8a3f2c/shared\n"
            f"- Today (UTC): {DATE}", out)

    def test_environment_standalone_fallbacks(self):
        out = gen.generate(cfg_standalone(), read(TEMPLATE), date=DATE)
        self.assertIn("- Team: standalone", out)
        cfg = cfg_standalone()
        cfg["storage_prefix"] = ""
        out = gen.generate(cfg, read(TEMPLATE), date=DATE)
        self.assertIn("- Storage prefix: agentteams", out)

    def test_no_residual_braces(self):
        for cfg in (cfg_team(), cfg_standalone()):
            out = gen.generate(cfg, read(TEMPLATE), date=DATE)
            self.assertNotIn("{{", out)
            self.assertNotIn("}}", out)

    def test_output_ends_with_single_newline(self):
        for extra in ({}, {"soul_text": SOUL_TEXT}):
            out = gen.generate(cfg_team(), read(TEMPLATE), date=DATE, **extra)
            self.assertTrue(out.endswith("\n"))
            self.assertFalse(out.endswith("\n\n"))

    def test_soul_crlf_normalized(self):
        out = gen.generate(cfg_team(), read(TEMPLATE), date=DATE,
                           soul_text="# SOUL\r\n\r\nbold\r\n")
        self.assertIn("\n\n## Persona\n\n", out)
        self.assertIn("\n\n# SOUL\n\nbold\n", out)
        self.assertNotIn("\r", out)

    def test_whitespace_only_soul_no_seam(self):
        out = gen.generate(cfg_team(), read(TEMPLATE), date=DATE,
                           soul_text="  \n\n")
        self.assertNotIn("## Persona", out)


class CliTest(unittest.TestCase):
    def run_cli(self, *args, env_extra=None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, os.path.join(REPO, "bridge",
                                          "generate_agent_md.py"), *args],
            capture_output=True, text=True, encoding="utf-8", env=env)

    def test_stdout_roundtrip(self):
        proc = self.run_cli("--runtime-config", RUNTIME_TEAM,
                            "--date", DATE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, read(GOLDEN_TEAM))

    def test_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "agent.md")
            proc = self.run_cli("--runtime-config", RUNTIME_STANDALONE,
                                "--date", DATE, "--output", out)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(read(out), read(GOLDEN_STANDALONE))

    def test_soul_and_profile_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            soul = os.path.join(tmp, "SOUL.md")
            profile = os.path.join(tmp, "PROFILE.md")
            with open(soul, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(SOUL_TEXT)
            with open(profile, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(PROFILE_TEXT)
            proc = self.run_cli("--runtime-config", RUNTIME_TEAM,
                                "--date", DATE,
                                "--soul-file", soul, "--profile-file", profile)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("## Persona", proc.stdout)
            self.assertIn("# SOUL", proc.stdout)
            self.assertIn("# PROFILE", proc.stdout)

    def test_missing_runtime_config_arg_is_usage_error(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)

    def test_bad_runtime_config_file_exit_1(self):
        proc = self.run_cli("--runtime-config", "does-not-exist.yaml")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error:", proc.stderr)

    def test_identity_error_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "runtime.yaml")
            with open(bad, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("member:\n  role: worker\n")
            proc = self.run_cli("--runtime-config", bad)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("member.name", proc.stderr)

    def test_template_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = os.path.join(tmp, "template.md")
            shutil.copy(TEMPLATE, broken)
            with open(broken, "a", encoding="utf-8", newline="\n") as fh:
                fh.write("\n{{LATE_ADDITION}}\n")
            proc = self.run_cli("--runtime-config", RUNTIME_TEAM,
                                "--date", DATE, "--template", broken)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("placeholder", proc.stderr)

    def test_unified_log_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            logfile = os.path.join(tmp, "agentteams.log")
            proc = self.run_cli(
                "--runtime-config", RUNTIME_TEAM, "--date", DATE,
                env_extra={"AGENTTEAMS_LOG_FILE": logfile,
                           "AGENTTEAMS_LOG_STDERR": "0"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            events = [ln for ln in read(logfile).splitlines()
                      if '"event": "generated"' in ln]
            self.assertEqual(len(events), 1)
            self.assertIn('"tool": "generate_agent_md"', events[0])
            self.assertIn('"worker_name": "t-8a3f2c-', events[0])
            self.assertIn('"coordinators": 1', events[0])


if __name__ == "__main__":
    unittest.main()
