import subprocess
import json
import re
import tempfile
from pathlib import Path
from typing_extensions import Annotated

from .modules import Tool, CTFCategories
from ..ctflogging import status

SCRIPT_DIR = Path(__file__).parent.parent.parent.resolve()
GHIDRA = SCRIPT_DIR / "ghidra_11.0.1_PUBLIC/support/analyzeHeadless"

class CommandExec(Tool):
    NAME = "run_command"
    def __init__(self, environment: "CTFEnvironment"):
        super().__init__()
        self.environment = environment

    @staticmethod
    def _clean(text):
        if text is None:
            return None
        return text.decode('utf-8', errors='backslashreplace').replace('\r\n', '\n')

    def run_cmd(self, command, timeout):
        """Run a command in the docker container and return
        {"stdout": stdout, "stderr": stderr, "returncode": returncode, "timed_out": timed_out}
        """
        p = subprocess.Popen(
            ['docker', 'exec', self.environment.container, 'bash', '-c', command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        try:
            stdout, stderr = p.communicate(timeout=timeout)
            out = self._clean(stdout)
            err = self._clean(stderr)
            return {"stdout": out, "stderr": err, "returncode": p.returncode, "timed_out": False}
        except subprocess.TimeoutExpired as e:
            p.kill()
            stdout, stderr = p.communicate(timeout=timeout)     
            out = self._clean(stdout)
            err = self._clean(stderr) 
            # out = self._clean(e.stdout)
            # err = self._clean(e.stderr)
            return {"stdout": out, "stderr": err, "returncode": None, "timed_out": True}
        except subprocess.CalledProcessError as e:
            out = self._clean(e.stdout)
            err = self._clean(e.stderr)
            return {"stdout": out, "stderr": err, "returncode": e.returncode, "timed_out": False}

    def __call__(self, command: Annotated[str, "the command to run"],
                       timeout: Annotated[float, "the maximum number of seconds to run the command"] = 10.0):
        """Execute a command in an Ubuntu container (persistent across calls)"""
        if command is None:
            return {"error": "No command provided"}
        return self.run_cmd(command, timeout)

class CheckFlag(Tool):
    NAME = "check_flag"
    def __init__(self, environment: "CTFEnvironment"):
        super().__init__()
        self.environment = environment
        self.real_flag = environment.challenge.flag

    def __call__(self, flag: Annotated[str,"the flag to check"]):
        """Check if a flag is correct."""
        if flag is None:
            return {"error": "No flag provided"}
        status.print(f"Checking flag:")
        status.print(f"  Provided: [blue]{flag}[/blue]", markup=True)
        status.print(f"    Actual: [green]{self.real_flag}[/green]", markup=True)
        if flag == self.real_flag:
            status.print(f"[red bold]Correct flag![/red bold]", markup=True)
            self.environment.solved = True
            return {"correct": True}
        else:
            status.print(f"[red bold]Incorrect flag.[/red bold]", markup=True)
            return {"correct": False}

class CreateFile(Tool):
    NAME = "createfile"
    def __init__(self, environment: "CTFEnvironment"):
        super().__init__()
        self.environment = environment

    def __call__(self,
                 path: Annotated[str,"path where the file should be created; relative paths will be relative to /home/ctfplayer/"],
                 contents: Annotated[str,"contents of the file"],
                 decode_escapes: Annotated[bool,"whether to decode escape sequences in the contents"] = False):
        """Create a file in the container with the given contents, returning the last 15 characters written"""
        if path is None:
            return {"error": "No path provided"}
        if contents is None:
            return {"error": "No contents provided"}
        if decode_escapes is None:
            decode_escapes = False
        return self.createfile(path, contents, decode_escapes)

    @staticmethod
    def _expanduser(path, home):
        """Expand ~ and ~user constructs in the given path"""
        strpath = str(path)
        if strpath.startswith('~'):
            strpath = strpath.replace('~', str(home), 1)
        return Path(strpath)

    def createfile(self, path, contents, decode_escapes=False):
        if decode_escapes:
            # Decode escape sequences to get a string
            try:
                decoded_contents = bytes(contents, 'utf-8').decode('unicode_escape')
            except UnicodeDecodeError as e:
                return {"error": f"Invalid escape sequence in contents: {e}"}
        else:
            decoded_contents = contents

        last_15_chars = decoded_contents[-15:]
        encoded_contents = decoded_contents.encode()

        path = Path(self._expanduser(path, self.environment.container_home))
        if not path.is_absolute():
            path = self.environment.container_home / path
        with tempfile.NamedTemporaryFile(mode="wb") as f:
            f.write(encoded_contents)
            f.flush()
            try:
                path = self.environment.copy_into_container(f.name, path)
                return {"success": True, "path": str(path), "last_15_chars": last_15_chars}
            except subprocess.CalledProcessError as e:
                return {"error": f"Error copying file into container: {e.stderr.decode('utf-8', errors='backslashreplace')}"}

class ContinueFile(Tool):
    NAME = "continuefile"

    def __init__(self, environment: "CTFEnvironment"):
        super().__init__()
        self.environment = environment

    def __call__(self,
                 path: Annotated[str, "Path where the file should be appended to; relative paths will be relative to /home/ctfplayer/"],
                 last_15_chars: Annotated[str, "The last 15 characters from the previous write"],
                 contents: Annotated[str, "Contents to append to the file"],
                 decode_escapes: Annotated[bool, "Whether to decode escape sequences in the contents"] = False):
        """Append to a file in the container with the given contents, verifying the last 15 characters"""
        if path is None:
            return {"error": "No path provided"}
        if contents is None:
            return {"error": "No contents provided"}
        if last_15_chars is None:
            return {"error": "No last_15_chars provided"}
        if decode_escapes is None:
            decode_escapes = False
        return self.append_file(path, contents, last_15_chars, decode_escapes)

    @staticmethod
    def _expanduser(path, home):
        """Expand ~ and ~user constructs in the given path"""
        strpath = str(path)
        if strpath.startswith('~'):
            strpath = strpath.replace('~', str(home), 1)
        return Path(strpath)

    def read_last_15_chars(self, path):
        """Read the last 15 characters of a file from the container"""
        command = f"tail -c 15 '{path}'"
        result = self.run_cmd(command, timeout=5)
        if result['returncode'] != 0:
            return {"error": f"Error reading file: {result['stderr']}"}
        return result['stdout']

    def run_cmd(self, command, timeout=10.0):
        """Run a command in the docker container"""
        p = subprocess.Popen(
            ['docker', 'exec', self.environment.container, 'bash', '-c', command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        try:
            stdout, stderr = p.communicate(timeout=timeout)
            return {
                "stdout": stdout.decode('utf-8', errors='backslashreplace'),
                "stderr": stderr.decode('utf-8', errors='backslashreplace'),
                "returncode": p.returncode,
                "timed_out": False
            }
        except subprocess.TimeoutExpired:
            p.kill()
            stdout, stderr = p.communicate()
            return {
                "stdout": stdout.decode('utf-8', errors='backslashreplace'),
                "stderr": stderr.decode('utf-8', errors='backslashreplace'),
                "returncode": None,
                "timed_out": True
            }

    def append_file(self, path, contents, last_15_chars, decode_escapes=False):
        # Expand the path
        expanded_path = str(self._expanduser(path, self.environment.container_home))
        if not expanded_path.startswith('/'):
            expanded_path = f"/home/ctfplayer/{expanded_path}"

        # Read last 15 characters of the existing file
        current_last_15 = self.read_last_15_chars(expanded_path)
        if "error" in current_last_15:
            return current_last_15
        if current_last_15 != last_15_chars:
            return {"error": "Last 15 characters do not match the file's existing content."}

        # Prepare new contents to append
        if decode_escapes:
            try:
                decoded_contents = bytes(contents, 'utf-8').decode('unicode_escape')
            except UnicodeDecodeError as e:
                return {"error": f"Invalid escape sequence in contents: {e}"}
        else:
            decoded_contents = contents

        # Safely append the content using printf to handle special characters
        safe_contents = decoded_contents.replace("'", "'\"'\"'")
        command = f"printf '%s' '{safe_contents}' >> '{expanded_path}'"
        result = self.run_cmd(command, timeout=5)
        if result['returncode'] != 0:
            return {"error": f"Error appending to file: {result['stderr']}"}

        # Read the new last 15 characters
        new_last_15 = self.read_last_15_chars(expanded_path)
        if "error" in new_last_15:
            return new_last_15

        return {"success": True, "path": expanded_path, "last_15_chars": new_last_15}

#class TakeNote(Tool):
#    NAME = "takenote"
#    
#    def __init__(self, environment: "CTFEnvironment"):
#        super().__init__()
#        self.environment = environment
#
#    def __call__(self, 
#                 note: Annotated[str, "Important discoveries to record"]):
#        """Record important discoveries"""
#        if not note:
#            return {"error": "No note provided"}
#        
#        self._notes.append(note)
#        return {"success": True, "message": "Note added"}


#class ReadNote(Tool):
#    NAME = "readnote"
#    
#    def __init__(self, environment: "CTFEnvironment"):
#        super().__init__()
#        self.environment = environment
#
#    def __call__(self):
#        """Review all important discoveries."""
#        if not self._notes:
#            return {"message": "No notes available"}
#        
#        return {"notes": self._notes}

class GiveUp(Tool):
    NAME = "give_up"
    def __init__(self, environment: "CTFEnvironment"):
        super().__init__()
        self.environment = environment

    def __call__(self, confirm: Annotated[bool,"a boolean flag to confirm that you want to give up"]):
        """Give up on the challenge"""
        if not confirm:
            return {"error": "You must confirm that you want to give up"}
        self.environment.giveup = True
        return {"success": True}

class Decompile(Tool):
    NAME = "decompile_function"
    CATEGORIES = {CTFCategories.rev, CTFCategories.pwn, CTFCategories.crypto}
    def __init__(self, environment: "CTFEnvironment"):
        super().__init__()
        self.environment = environment
        self.challenge = self.environment.challenge
        self._decomp_cache = {}

    def __call__(self,
                 path: Annotated[str,"path to the binary to decompile"],
                 function: Annotated[str,"the function to decompile"] = 'main'):
        """Decompile a function from a binary using Ghidra."""
        if path is None:
            return {"error": "No binary provided"}
        if function is None:
            function = "main"
        return self.decompile(path, function)

    def find_function(self, dis, function):
        if function in dis["functions"]:
            return dis["functions"][function]
        # Looking for main entry point, so try other names also
        if function == "main":
            if "_start" in dis["functions"]:
                return dis["functions"]["_start"]
            if "invoke_main" in dis["functions"]:
                return dis["functions"]["invoke_main"]
        # Check if requesting radare2 unnamed function with address
        if re.match(r"fcn\.[0-9a-f]+$", function):
            addr = function[4:]
            if addr in dis["addresses"]:
                return dis["functions"][dis["addresses"][addr]]
        # Nothing found
        return None

    def decompile(self, binary, function):
        # Look for the decompilation output in "decomp"
        basename = Path(binary).name
        if basename not in self._decomp_cache:
            decomp_output = SCRIPT_DIR / f"decomp/{self.challenge.category}/{self.challenge.challenge_dir.name}/{basename}.decomp.json"
            if decomp_output.exists():
                self._decomp_cache[basename] = json.loads(decomp_output.read_text())
            else:
                if not self.run_ghidra(basename, decomp_output):
                    return {"error": f"Decompilation for {binary} not available"}
                self._decomp_cache[basename] = json.loads(decomp_output.read_text())

        if found := self.find_function(self._decomp_cache[basename], function):
            return {"decompilation": found}
        else:
            return {"error": f"Function {function} not found in {binary}"}

    def run_ghidra(self, binary, output):
        status.debug_message(f"Running Ghidra to decompile {binary}...")
        binary_paths = self.challenge.challenge_dir.glob(f'**/{binary}')
        real_binary = next(binary_paths, None)
        if not real_binary or not real_binary.exists():
            return False
        status.debug_message(f"Real binary path: {real_binary}")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            subprocess.run(
                [GHIDRA, tmpdir, "DummyProj", "-scriptpath", SCRIPT_DIR / 'llm_ctf/ghidra_scripts',
                 "-import", real_binary, "-postscript", "DecompileToJson.java", output],
                check=False, capture_output=True,
            )
            return output.exists()

class Disassemble(Tool):
    NAME = "disassemble_function"
    CATEGORIES = {CTFCategories.rev, CTFCategories.pwn, CTFCategories.crypto}
    def __init__(self, environment: "CTFEnvironment"):
        super().__init__()
        self.environment = environment
        self.challenge = self.environment.challenge
        self._disasm_cache = {}

    def __call__(self,
                 path: Annotated[str,"path to the binary to disassemble"],
                 function: Annotated[str,"the function to disassemble"] = 'main'):
        """Disassemble a function from a binary using Ghidra."""
        if function is None:
            function = "main"
        if path is None:
            return {"error": "No binary provided"}
        return self.disassemble(path, function)

    def find_function(self, dis, function):
        if function in dis["functions"]:
            return dis["functions"][function]
        # Looking for main entry point, so try other names also
        if function == "main":
            if "_start" in dis["functions"]:
                return dis["functions"]["_start"]
            if "invoke_main" in dis["functions"]:
                return dis["functions"]["invoke_main"]
        # Check if requesting radare2 unnamed function with address
        if re.match(r"fcn\.[0-9a-f]+$", function):
            addr = function[4:]
            if addr in dis["addresses"]:
                return dis["functions"][dis["addresses"][addr]]
        # Nothing found
        return None

    def disassemble(self, binary, function):
        # Look for the disassembly output in "decomp"
        basename = Path(binary).name
        disasm_output = SCRIPT_DIR / f"decomp/{self.challenge.category}/{self.challenge.challenge_dir.name}/{basename}.disas.json"

        if basename not in self._disasm_cache:
            if disasm_output.exists():
                self._disasm_cache[basename] = json.loads(disasm_output.read_text())
            else:
                if not self.run_ghidra(basename, disasm_output):
                    return {"error": f"Disassembly for {binary} not available"}
                self._disasm_cache[basename] = json.loads(disasm_output.read_text())

        if found := self.find_function(self._disasm_cache[basename], function):
            return {"disassembly": found}
        else:
            return {"error": f"Function {function} not found in {binary}"}

    def run_ghidra(self, binary, output):
        status.debug_message(f"Running Ghidra to disassemble {binary}...")
        binary_paths = self.challenge.challenge_dir.glob(f'**/{binary}')
        real_binary = next(binary_paths, None)
        if not real_binary or not real_binary.exists():
            return False
        status.debug_message(f"Real binary path: {real_binary}")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            subprocess.run(
                [GHIDRA, tmpdir, "DummyProj", "-scriptpath", SCRIPT_DIR / 'llm_ctf/ghidra_scripts',
                 "-import", real_binary, "-postscript", "DisassembleToJson.java", output],
                check=False, capture_output=True,
            )
            return output.exists()
