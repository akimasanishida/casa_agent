import asyncio
import json
import os
import subprocess

from agents import (Agent, FileSearchTool, MaxTurnsExceeded, Runner,
                    function_tool)
from dotenv import load_dotenv
from openai.types.responses import ResponseTextDeltaEvent
from prompt_toolkit import PromptSession


@function_tool
def exec_command(container_name: str, command: str) -> str:
    """
    Execute a shell command in the container.

    Args:
        container_name (str): The name of the container.
        command (str): The command to execute.

    Returns:
        str: The STDOUT and STDERR of the command.
    """
    print(f"\n\tCommand: {command}\n")
    try:
        cmd = ["podman", "exec", "-i", container_name, "bash", "-c", command]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        return output
    except subprocess.CalledProcessError as e:
        return f"Error executing command: {e}"


@function_tool
def write_file(container_name: str, mode: str, file_path: str, content: str) -> str:
    """
    Write to a file in the container.

    Args:
        container_name (str): The name of the container.
        mode (str): The mode of the file operation. "w" for write, "a" for append.
        file_path (str): The path to the file.
        content (str): The content to write to the file (only used in 'write' mode).
    """
    mode_str = "write" if mode == "w" else "append"
    print(
        f'\n\tWrite to file "{file_path}" as mode "{mode_str}"\nContent:\n{content}\n'
    )
    try:
        python_cmd = f"with open({json.dumps(file_path)}, {json.dumps(mode)}) as f: f.write({json.dumps(content)})"
        cmd = ["podman", "exec", "-i", container_name, "python", "-c", python_cmd]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        output = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        return output
    except subprocess.CalledProcessError as e:
        return f"Error executing command: {e}"


class CasaAgents:
    def __init__(self):
        self._casa_dir = "/usr/local/casa/casa-6.6.1-17-pipeline-2024.1.0.8/"
        self._podman_path = "podman"
        self._image_name = "casa-skeleton-python"
        self._analysisUtils_dir = "/home/skrbcr/analysis_scripts/"

        # Check if podman is installed
        try:
            subprocess.run(
                [self._podman_path, "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            raise RuntimeError(
                f"{self._podman_path} is not installed. Please install it."
            )

        # Check if casa is installed
        self._casa_path = os.path.join(self._casa_dir, "bin/casa")
        try:
            subprocess.run(
                [self._casa_path, "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            raise RuntimeError(
                f"Cannot execute {self._casa_path}. Please install it or check the path."
            )

        # Set the container name
        self._container_name = "casa-agent-" + os.urandom(4).hex()

        # Create a new containe
        cmd = [
            "podman",
            "run",
            "-d",
            # Give the container a name
            "--name",
            self._container_name,
            # Use X11
            "-e",
            f"DISPLAY={os.environ['DISPLAY']}",
            "-e",
            "QT_X11_NO_MITSHM=1",
            "-v",
            "/tmp/.X11-unix:/tmp/.X11-unix",
            # Use FUSE
            "--device",
            "/dev/fuse",
            "--cap-add=SYS_ADMIN",
            "--security-opt",
            "label=disable",
            # Mount the working directory as read-write
            "-v",
            "./test_dir:/working:rw",  # For testing
            # Mount the casa directory as read-only
            "-v",
            f"{self._casa_dir}:/opt/casa:ro",
            # Mount the analysisUtils directory as read-only
            "-v",
            f"{self._analysisUtils_dir}:/opt/analysisUtils:ro",
            # Corresponding the uid
            "--userns=keep-id",
            # cd to the working directory
            "--workdir",
            "/working",
            # Image name
            self._image_name,
            # Do nothing
            "tail",
            "-f",
            "/dev/null",
        ]

        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Cannot create container {self._container_name}.\n{e}")

        with open("./systemPrompt.md", "r") as f:
            system_prompt = f.read()

        load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
        vector_store_id = os.environ.get("VECTOR_STORE_ID")

        # Initialize the agent
        self._agent = Agent(
            name="casa-agent",
            model="gpt-4.1",
            instructions=system_prompt
            + f"You can operate the command by calling exec_command and the container name is {self._container_name}. You can use common commands in this bash. The path of Python is `python`. The path of CASA is `/opt/casa/bin/casa`. analysisUtils is `/opt/analysisUtils`.",
            tools=[
                exec_command,
                write_file,
                FileSearchTool(
                    vector_store_ids=[vector_store_id],
                ),
            ],
        )
        self._previous_response_id = None

        self._session = PromptSession()

    async def _run_loop(self) -> None:
        while True:
            prompt = await self._session.prompt_async("> ")

            cmd = prompt.strip().lower()
            if cmd in ["exit", "quit", "q"]:
                break

            # Run the agent
            try:
                result = await Runner.run(
                    self._agent,
                    input=prompt,
                    previous_response_id=self._previous_response_id,
                    max_turns=50,
                )

                self._previous_response_id = result.last_response_id
                reply = result.final_output
                print(f"Agent: {reply}")
            except MaxTurnsExceeded as _:
                print(
                    f"The agent has exceeded the maximum number of turns.\nIf you want to continue, please tell me."
                )

    def run(self) -> None:
        """
        Run the agent with the given prompt.

        Args:
            None

        Returns:
            None
        """
        asyncio.run(self._run_loop())

    def close(self):
        """
        Close the agent and clean up the container.
        """
        # Stop the container
        cmd = ["podman", "kill", self._container_name]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError:
            print(f"Cannot stop container {self._container_name}.")

        # Clean up the container
        cmd = ["podman", "rm", "-f", self._container_name]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError:
            print(f"Cannot remove container {self._container_name}.")


if __name__ == "__main__":
    agent = CasaAgents()
    # prompt = "現在のディクトリにあるmeasurement set とそのサイズをGB で表示してください。"
    # prompt = "CASA のバージョンを表示してください。"
    agent.run()
    agent.close()
