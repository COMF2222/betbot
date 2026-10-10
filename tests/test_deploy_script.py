"""Execute the real Bash CLI against a Docker stub, without starting containers."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [
    "compose",
    "--project-name",
    "betbot",
    "--env-file",
    "deploy.env",
    "-f",
    "compose.yaml",
    "-f",
    "compose.mihomo.yaml",
]
DOCKER_STUB = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["DEPLOY_TEST_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args == ["info"] or args == ["compose", "version"]:
    pass
elif args[0] == "ps":
    print("vpn-mihomo metacubex/mihomo:latest")
elif args[0] == "inspect":
    print("true" if args[2] == "{{.State.Running}}" else "bridge")
elif args[0] == "exec":
    print("7890" if "sed -n" in args[4] else "mixed-port: 7890")
elif args[:9] == ["compose", "--project-name", "betbot", "--env-file", "deploy.env",
                  "-f", "compose.yaml", "-f", "compose.mihomo.yaml"]:
    if len(args) < 10 or args[9] not in {"config", "build", "run", "up", "stop", "logs", "ps"}:
        sys.exit("Invalid Compose subcommand: " + repr(args))
    if args[9] == "run" and "-T" in args:
        sys.stdin.read()
else:
    sys.exit("Unexpected Docker command: " + repr(args))
'''


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "Linux Bash")
class DeployScriptTest(unittest.TestCase):
    def test_all_commands_pass_separate_arguments_to_compose(self):
        commands = {
            "setup": ["config", "build", "run", "run"],
            "check": ["run"],
            "inspect": ["run"],
            "start": ["up", "ps"],
            "stop": ["stop"],
            "restart": ["up"],
            "logs": ["logs"],
            "status": ["ps"],
        }
        for action, expected in commands.items():
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                stage = Path(directory)
                shutil.copyfile(ROOT / "deploy.sh", stage / "deploy.sh")
                (stage / ".env").write_text("", encoding="utf-8")
                (stage / "data").mkdir()
                (stage / "data/model.json").write_text("{}", encoding="utf-8")
                if action != "setup":
                    (stage / "deploy.env").write_text("MIHOMO_CONTAINER=vpn-mihomo\n")
                stub = stage / "docker"
                stub.write_text(DOCKER_STUB, encoding="utf-8")
                stub.chmod(0o755)
                log = stage / "commands.jsonl"
                result = subprocess.run(
                    ["bash", str(stage / "deploy.sh"), action],
                    env={
                        **os.environ,
                        "PATH": str(stage) + os.pathsep + os.environ["PATH"],
                        "DEPLOY_TEST_LOG": str(log),
                    },
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                compose_calls = [call for call in calls if call[:9] == COMPOSE]
                self.assertEqual([call[9] for call in compose_calls], expected)
                for call in compose_calls:
                    self.assertNotIn("\n", " ".join(call))
                if action == "setup":
                    self.assertIn(
                        "MIHOMO_NETWORK_MODE=container:vpn-mihomo",
                        (stage / "deploy.env").read_text(),
                    )
                if action in {"start", "stop", "restart"}:
                    self.assertIn("betbot", compose_calls[0][10:])
                    self.assertNotIn("down", compose_calls[0])


if __name__ == "__main__":
    unittest.main()
