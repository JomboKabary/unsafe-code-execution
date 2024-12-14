import asyncio
import json
import os
import subprocess
import sys
import tempfile
import typing
import re
from pydantic import BaseModel, Field

class Action:
    class Valves(BaseModel):
        MAX_RUNTIME_SECONDS: int = Field(
            default=30, description="Maximum number of seconds code is given to run."
        )
        DEBUG: bool = Field(
            default=False, description="Whether to produce debug logs during execution."
        )
    def __init__(self):
        self.valves = self.Valves()

    async def action(
        self,
        body: dict,
        __event_emitter__: typing.Callable[[dict], typing.Any] = None,
        __user__=None,
    ) -> typing.Optional[dict]:
        valves = self.valves
        debug = valves.DEBUG
        emitter = EventEmitter(__event_emitter__, debug=debug)

        async def _fail(error_message, status="SANDBOX_ERROR"):
            if debug:
                await emitter.fail(
                    f"[DEBUG MODE] {error_message}; body={body}; valves=[{valves}]"
                )
            else:
                await emitter.fail(error_message)
            return {"status": status, "output": error_message}

        if not body.get("messages"):
            return await _fail("No messages in conversation.", status="INVALID_INPUT")
        last_message = body["messages"][-1]
        if last_message["role"] != "assistant":
            return await _fail(
                "Last message was not from the AI model.", status="INVALID_INPUT"
            )
        split_three_backticks = last_message["content"].split("```")
        if len(split_three_backticks) < 3 or len(split_three_backticks) % 2 != 1:
            return await _fail(
                "Last message did not contain well-formed code blocks.",
                status="INVALID_INPUT",
            )
        chosen_code_block = None
        language = None
        for code_block in split_three_backticks[-2:0:-2]:
            if code_block.startswith("python\n") or code_block.startswith("python3\n"):
                chosen_code_block = code_block
                language = "python"
            elif code_block.startswith("bash\n") or code_block.startswith("sh\n") or code_block.startswith("shell\n"):
                chosen_code_block = code_block
                language = "bash"
                break
        if not chosen_code_block:
            last_code_block = split_three_backticks[-2]
            first_line = last_code_block.strip().split("\n")[0]
            if first_line.startswith("#!") and (
                first_line.endswith("python") or first_line.endswith("python3")
            ):
                chosen_code_block = last_code_block
                language = "python"
            elif first_line.startswith("#!") and first_line.endswith("sh"):
                chosen_code_block = last_code_block
                language = "bash"
            elif any(
                python_like in last_code_block
                for python_like in ("import ", "print(", "print ")
            ):
                chosen_code_block = last_code_block
                language = "python"
            elif any(
                bash_like in last_code_block
                for bash_like in ("echo ", "if [", "; do", "esac\n")
            ):
                chosen_code_block = last_code_block
                language = "bash"
        if not chosen_code_block:
            return await _fail(
                "Message does not contain code blocks detected as Python or Bash."
            )

        try:
            code = chosen_code_block
            if language == "python":
                code = code.removeprefix("python3").removeprefix("python")
            elif language == "bash":
                code = code.removeprefix("shell").removeprefix("bash").removeprefix("sh")
            code = code.strip()
            language_title = language.title()
            execution_tracker = CodeExecutionTracker(
                name=f"{language_title} code block", code=code, language=language
            )
            await emitter.clear_status()
            await emitter.code_execution(execution_tracker)
            with tempfile.TemporaryDirectory(prefix="code_exec_") as tmp_dir:
                output = None
                status = "UNKNOWN"
                try:
                   interpreter_path = None
                   if language == "bash":
                       interpreter_path = "/bin/bash"
                   elif language == "python":
                        interpreter_path = sys.executable
                   if not interpreter_path:
                       raise RuntimeError(f"Cannot find interpreter for language: {language}")
                   result = subprocess.run(
                        [interpreter_path, "/dev/stdin"],
                        input=code + "\n",
                        text=True,
                        capture_output=True,
                        timeout=valves.MAX_RUNTIME_SECONDS,
                        check=False,
                    )
                   if result.returncode != 0:
                      status="ERROR"
                      output=result.stderr
                      raise subprocess.CalledProcessError(returncode=result.returncode, cmd=[interpreter_path, "/dev/stdin"], stderr=result.stderr, output=result.stdout)
                   else:
                       status="OK"
                       output=result.stdout
                except subprocess.TimeoutExpired as e:
                    await emitter.fail(
                        f"Code timed out after {valves.MAX_RUNTIME_SECONDS} seconds"
                    )
                    execution_tracker.set_error(
                        f"Code timed out after {valves.MAX_RUNTIME_SECONDS} seconds"
                    )
                    status = "TIMEOUT"
                    output = e.stderr
                except subprocess.CalledProcessError as e:
                    await emitter.fail(f"{language_title}: {e}")
                    execution_tracker.set_error(f"{language_title}: {e}")
                    status = "ERROR"
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
                        f"\n\n---\nI executed this {language_title} code and got:\n```Output\n{output}\n```\n"
                    )
                  return {
                        "status": status,
                        "output": output,
                    }
                elif status == "TIMEOUT":
                    if output:
                        await emitter.message(
                            f"\n\n---\nI executed this {language_title} code and it timed out after {self.valves.MAX_RUNTIME_SECONDS} seconds:\n```Error\n{output}\n```\n"
                        )
                    else:
                        await emitter.message(
                            f"\n\n---\nI executed this {language_title} code and it timed out after {self.valves.MAX_RUNTIME_SECONDS} seconds.\n"
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
                    raise RuntimeError(
                        f"Unexplained status: {status} (output: {output})"
                    )
                return { "status": status, "output": output }
        except Exception as e:
            return await _fail(f"Unhandled exception: {e}")


class EventEmitter:
    def __init__(
        self,
        event_emitter: typing.Callable[[dict], typing.Any] = None,
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
            if asyncio.isfuture(maybe_future) or isinstance(maybe_future, typing.Awaitable):
                result = await maybe_future
        return result

    async def status(
        self, description="Unknown state", status="in_progress", done=False
    ):
        self._emitted_status = True
        if self._status_prefix is not None:
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
        description="Run arbitrary code in a gVisor sandbox."
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

        action = Action()
        code = sys.stdin.read()
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": f"```{args.language}\n{code}\n```\n",
                },
            ],
        }
        os.environ[
            Action.Valves()._VALVE_OVERRIDE_ENVIRONMENT_VARIABLE_NAME_PREFIX + "DEBUG"
        ] = str(args.debug).lower()
        output_str = await action.action(body=body, __event_emitter__=_dummy_emitter)
        print(output_str)

    asyncio.run(_local_run())
