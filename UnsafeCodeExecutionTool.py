import asyncio
import json
import os
import subprocess
import sys
import tempfile
import typing
import re
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        MAX_RUNTIME_SECONDS: int = Field(
            default=30, description="Maximum number of seconds code is given to run."
        )
        DEBUG: bool = Field(
            default=False, description="Whether to produce debug logs during execution."
        )

    def __init__(self):
        self.valves = self.Valves()

    async def run_bash_command(
        self,
        bash_command: str,
        __event_emitter__: typing.Any = None,
    ) -> str:
        """
        Run a bash command-line or script.
        """
        result = await self._run_code(
            language="bash",
            code=bash_command,
            event_emitter=__event_emitter__,
        )
        return json.dumps(
            {
                "bash_command": bash_command,
                "status": result["status"],
                "output": result["output"],
            },
            ensure_ascii=False,
        )

    async def run_python_code(
        self,
        python_code: str,
        __event_emitter__: typing.Any = None,
    ) -> str:
        """
        Run Python code.
        """
        result = await self._run_code(
            language="python",
            code=python_code,
            event_emitter=__event_emitter__,
        )
        return json.dumps(
            {
                "python_code": python_code,
                "status": result["status"],
                "output": result["output"],
            },
            ensure_ascii=False,
        )

    async def _run_code(
        self,
        language: str,
        code: str,
        event_emitter: typing.Any = None,
    ) -> dict:
        """
        Run code.
        """
        valves = self.valves
        debug = valves.DEBUG
        emitter = EventEmitter(event_emitter, debug=debug)
        execution_tracker: typing.Optional[CodeExecutionTracker] = None

        async def _fail(error_message, status="SANDBOX_ERROR"):
            if execution_tracker:
                execution_tracker.set_error(error_message)
                await emitter.code_execution(execution_tracker)
            if debug:
                await emitter.fail(
                    f"[DEBUG MODE] {error_message}; language={language}; code={code}; valves=[{valves}]"
                )
            else:
                await emitter.fail(error_message)
            return {"status": status, "output": error_message}

        try:
            status = "UNKNOWN"
            output = None
            language_title = language.title()

            code = code.strip()
            code = code.removeprefix("```" + language).removeprefix("```").removesuffix("```")
            code = code.strip()
            code = code.strip("`").strip()

            execution_tracker = CodeExecutionTracker(
                name=f"{language_title} tool execution", code=code, language=language
            )
            await emitter.clear_status()
            await emitter.code_execution(execution_tracker)

            with tempfile.TemporaryDirectory(prefix="code_exec_") as tmp_dir:
                interpreter_path = None
                if language == "bash":
                    interpreter_path = "/bin/bash"
                elif language == "python":
                    interpreter_path = sys.executable
                if not interpreter_path:
                    raise RuntimeError(
                        f"Cannot find interpreter for language: {language}"
                    )
                try:
                  result = subprocess.run(
                      [interpreter_path, "/dev/stdin"],
                      input=code + "\n",
                      text=True,
                      capture_output=True,
                      timeout=valves.MAX_RUNTIME_SECONDS,
                      check=False,
                  )
                  if result.returncode != 0:
                      status = "ERROR"
                      output = result.stderr
                  else:
                    status = "OK"
                    output = result.stdout
                except subprocess.TimeoutExpired as e:
                    await emitter.fail(
                        f"Code timed out after {valves.MAX_RUNTIME_SECONDS} seconds"
                    )
                    execution_tracker.set_error(
                        f"Code timed out after {valves.MAX_RUNTIME_SECONDS} seconds"
                    )
                    status = "TIMEOUT"
                    output = e.stderr
                if output:
                    output = output.strip()
                execution_tracker.set_output(output)
                await emitter.code_execution(execution_tracker)
                if debug:
                  await emitter.status(
                        status="complete" if status == "OK" else "error",
                        done=True,
                        description=f"[DEBUG MODE] status={status}; output={output}; valves=[{valves}]",
                    )
                if status == "OK":
                    await emitter.message(
                        f"\n<details>\n<summary>Code Execution</summary>\nI executed the following {language} code:\n```{language}\n{code}\n```\n```Output\n{output.strip()}\n```\n</details>\n"
                    )
                elif status == "TIMEOUT":
                    if output:
                        await emitter.message(
                            f"\n\n---\nI executed this {language_title} code and it timed out after {valves.MAX_RUNTIME_SECONDS} seconds:\n```Error\n{output}\n```\n"
                        )
                    else:
                        await emitter.message(
                            f"\n\n---\nI executed this {language_title} code and it timed out after {valves.MAX_RUNTIME_SECONDS} seconds.\n"
                        )
                elif status == "ERROR" and output:
                    await emitter.message(
                        f"\n\n---\nI executed this {language_title} code and got the following error:\n```Error\n{output}\n```\n"
                    )
                elif status == "ERROR":
                    await emitter.message(
                        f"\n\n---\nI executed this {language_title} code but got an unexplained error.\n"
                    )
                else:
                  raise RuntimeError(f"Unexplained status: {status} (output: {output})")

                await emitter.code_execution(execution_tracker)
                return {
                    "status": status,
                    "output": output,
                }
        except Exception as e:
            return await _fail(f"Unhandled exception: {e}")


class EventEmitter:
    def __init__(
        self,
        event_emitter: typing.Any = None,
        debug: bool = False,
    ):
        self.event_emitter = event_emitter
        self._debug = debug
        self._status_prefix = None
        self._emitted_status = False

    def set_status_prefix(self, status_prefix):
        self._status_prefix = status_prefix

    async def _emit(self, typ, data, twice):
        if self._debug:
            print(f"Emitting {typ} event: {data}", file=sys.stderr)
        if not self.event_emitter:
            return None
        result = None
        for _ in range(2 if twice else 1):
            maybe_future = self.event_emitter(
                {
                    "type": typ,
                    "data": data,
                }
            )
            if asyncio.isfuture(maybe_future) or isinstance(
                maybe_future, typing.Awaitable
            ):
                result = await maybe_future
        return result

    async def status(
        self, description="Unknown state", status="in_progress", done=False
    ):
        self._emitted_status = True
        if self._status_prefix:
            description = f"{self._status_prefix}{description}"
        await self._emit(
            "status",
            {
                "status": status,
                "description": description,
                "done": done,
            },
            twice=not done and len(description) <= 1024,
        )

    async def fail(self, description="Unknown error"):
        await self.status(description=description, status="error", done=True)

    async def clear_status(self):
        if not self._emitted_status:
            return
        self._emitted_status = False
        await self._emit(
            "status",
            {
                "status": "complete",
                "description": "",
                "done": True,
            },
            twice=True,
        )

    async def message(self, content):
        await self._emit(
            "message",
            {
                "content": content,
            },
            twice=False,
        )

    async def code_execution(self, code_execution_tracker):
        await self._emit(
            "citation", code_execution_tracker._citation_data(), twice=True
        )


class CodeExecutionTracker:
    def __init__(self, name, code, language):
        self._uuid = str(id(self))
        self.name = name
        self.code = code
        self.language = language
        self._result = {}

    def set_error(self, error):
        self._result["error"] = error

    def set_output(self, output):
        self._result["output"] = output

    def _citation_data(self):
        data = {
            "type": "code_execution",
            "id": self._uuid,
            "name": self.name,
            "code": self.code,
            "language": self.language,
        }
        if "output" in self._result or "error" in self._result:
            data["result"] = self._result
        return data


# Debug utility: Run code from stdin if running as a normal Python script.
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run arbitrary code."
    )
    parser.add_argument(
        "--language",
        choices=("python", "bash"),
        default="python",
        help="Language of the code to run.",
    )
    parser.add_argument(
        "--debug", action="store_true", default=False, help="Enable debug mode."
    )

    args = parser.parse_args()


    async def _local_run():
        def _dummy_emitter(event):
            print(f"Event: {event}", file=sys.stderr)

        tools = Tools()
        code = sys.stdin.read()
        if args.language == "bash":
            output_str = await tools.run_bash_command(
                bash_command=code, __event_emitter__=_dummy_emitter
            )
        else:
            output_str = await tools.run_python_code(
                python_code=code, __event_emitter__=_dummy_emitter
            )
        print(output_str)

    asyncio.run(_local_run())
