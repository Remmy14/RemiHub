"""Real validator subprocess outcomes, independent of reviewer decision parsing."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from backend.core.codex_implementation import (CommandImplementationValidator,
    ImplementationValidationFailure, ImplementationValidationExecutionError)
from tests.test_agent_worker import claimed_run
from backend.core.agent_state import RunPhase


class ValidationInteractionTests(unittest.TestCase):
    def run_validator(self,source,*,timeout=5):
        with tempfile.TemporaryDirectory(prefix='review-validator-fixture-') as temporary:
            root=Path(temporary);program=root/'validator';program.write_text('#!/usr/bin/python3\n'+source+'\n');program.chmod(0o755)
            validator=CommandImplementationValidator(command=program,timeout_seconds=timeout)
            return validator.validate(claim=claimed_run(phase=RunPhase.IMPLEMENTATION),workspace=SimpleNamespace(path=root))

    def test_trusted_success_failure_and_infrastructure_error_remain_distinct(self):
        self.assertTrue(self.run_validator('print(\'{"success": true}\')')['success'])
        with self.assertRaises(ImplementationValidationFailure):
            self.run_validator('import sys\nprint(\'{"success": false, "checks": [{"status":"failed"}]}\')\nsys.exit(1)')
        for source in ['raise OSError("fixture process crash")','print("malformed JSON")']:
            with self.subTest(source=source),self.assertRaises(ImplementationValidationExecutionError):
                self.run_validator(source)
        with self.assertRaises(ImplementationValidationExecutionError):
            self.run_validator('import time\ntime.sleep(2)',timeout=1)

if __name__=='__main__':unittest.main()
