#!/usr/bin/env python3

from argparse import Namespace
import json
from openai import OpenAI
import os
from typing import List, Optional, Tuple
import tiktoken

from ..formatters import Formatter
from .backend import Backend
from ..tools.manager import Tool, ToolCall, ToolResult
from ..ctflogging import status
from openai import RateLimitError
from openai.types.chat import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall as OAIToolCall
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam
from .utils import KEYS, MODEL_INFO

import backoff  # for exponential backoff

API_KEY_PATH = "~/.openai/api_key"

def get_tool_calls(otc_calls : List[OAIToolCall]) -> List[ToolCall]:
    return [ToolCall.create_unparsed(otc.function.name, otc.id, otc.function.arguments) for otc in otc_calls]

def make_call_result(res : ToolResult):
    return dict(
        name=res.name,
        role="tool",
        content=json.dumps(res.result),
        tool_call_id=res.id,
    )

def count_token(message: str, model: str):
    if not message:
        return 0
    enc = tiktoken.encoding_for_model(model_name=model)
    num_tokens = len(enc.encode(message))
    return num_tokens

class OpenAIBackend(Backend):
    NAME = 'openai'
    MODELS = list(MODEL_INFO[NAME].keys())

    def __init__(self, system_message : str, hint_message: str, tools: dict[str,Tool], args : Namespace):
        self.args = args
        if args.api_key is None:
            if KEYS and "OPENAI_API_KEY" in KEYS:
                api_key = KEYS["OPENAI_API_KEY"].strip()
            if "OPENAI_API_KEY" in os.environ:
                api_key = os.environ["OPENAI_API_KEY"]
            elif os.path.exists(os.path.expanduser(API_KEY_PATH)):
                api_key = open(os.path.expanduser(API_KEY_PATH), "r").read().strip()
            else:
                raise ValueError(f"No OpenAI API key provided and none found in OPENAI_API_KEY or {API_KEY_PATH}")
        self.client = OpenAI(api_key=api_key)
        self.tools = tools
        self.tool_schemas = [ChatCompletionToolParam(**tool.schema) for tool in tools.values()]
        if args.model:
            if args.model not in self.MODELS:
                raise ValueError(f"Invalid model {args.model}. Must be one of {self.MODELS}")
            self.model = args.model
        else:
            self.model = self.MODELS[0]
            # Update the args object so that the model name will be included in the logs
            args.model = self.model
        self.system_message = system_message
        self.hint_message = hint_message
        self.messages += self.get_initial_messages()
        self.in_price = MODEL_INFO[self.NAME][self.args.model].get("cost_per_input_token", 0)
        self.out_price = MODEL_INFO[self.NAME][self.args.model].get("cost_per_output_token", 0)

    def setup(self):
        status.system_message(self.system_message)
        if self.args.hints:
            status.hint_message(self.hint_message)

    def get_initial_messages(self):
        messages = [
            self._system_message(self.system_message),
        ]
        if self.args.hints:
            messages.append(self._hint_message(self.hint_message))
        return messages

    @classmethod
    def get_models(cls):
        return cls.MODELS

    @backoff.on_exception(backoff.expo, RateLimitError, max_tries=5)
    def _call_model(self) -> ChatCompletionMessage:
        return self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=self.tool_schemas,
            tool_choice="auto",
        ).choices[0].message

    def _message(self, content : str, role : str) -> dict[str,str]:
        return {
            "role": "user" if role == "hint" else role,
            "content": content,
            "hint": role == 'hint',
        }

    def _user_message(self, content : str) -> dict[str,str]:
        return self._message(content, "user")

    def _system_message(self, content : str) -> dict[str,str]:
        return self._message(content, "system")
    
    def _hint_message(self, content: str) -> dict[str, str]:
        return self._message(content, "hint")

    def send(self, message : str) -> Tuple[Optional[str],bool]:
        self.messages.append(self._user_message(message))
        response = self._call_model()
        self.messages.append(response)
        in_token = count_token(message=message, model=self.args.model)
        out_token = count_token(message=response.content, model=self.args.model)
        cost = in_token * self.in_price + out_token * self.out_price
        return response.content, bool(response.tool_calls), cost

    def run_tools(self) -> Tuple[Optional[str],bool]:
        tool_calls = get_tool_calls(self.messages[-1].tool_calls)
        tool_results = []
        for tool_call in tool_calls:
            # Tool lookup
            function_name = tool_call.name
            tool = self.tools.get(function_name)
            if not tool:
                tool_res = tool_call.error(f"Unknown tool {function_name}")
                tool_results.append(make_call_result(tool_res))
                continue

            # Parameter parsing
            try:
                arguments = self.extract_parameters(tool, tool_call)
            except json.JSONDecodeError as e:
                status.debug_message(f"Error decoding arguments for {function_name}: {e}")
                status.debug_message(f"Arguments: {tool_call.function.arguments}")
                tool_res = tool_call.error(f"{type(e).__name__} decoding arguments for {function_name}: {e}")
                tool_results.append(make_call_result(tool_res))
                continue
            except ValueError as e:
                status.debug_message(f"Error extracting parameters for {function_name}: {e}")
                tool_res = tool_call.error(
                    f"{type(e).__name__} extracting parameters for {function_name}: {e}"
                )
                tool_results.append(make_call_result(tool_res))
                continue

            # Tool execution
            status.debug_message(f"Calling {arguments}")
            tool_res_plain = tool.run(arguments)
            status.debug_message(f"Result:")
            for k,v in tool_res_plain.result.items():
                status.debug_message(f"{k}:\n{v}")
            # status.debug_message(f"=> {tool_res_plain.result}", truncate=True)
            try:
                tool_res = make_call_result(tool_res_plain)
            except TypeError as e:
                status.debug_message(f"Error encoding results from {function_name}: {e}")
                tool_res = make_call_result(tool_call.error(
                    f"{type(e).__name__} running {function_name}: {e}"
                ))
            except Exception as e:
                status.debug_message(f"Error running {function_name}: {e}")
                tool_res = make_call_result(tool_call.error(
                    f"{type(e).__name__} running {function_name}: {e}"
                ))
            tool_results.append(tool_res)
        self.messages += tool_results
        response = self._call_model()
        out_token = count_token(message=response.content, model=self.args.model)
        cost = out_token * self.out_price
        self.messages.append(response)
        return response.content, bool(response.tool_calls), cost

    def extract_parameters(self, tool : Tool, tool_call : ToolCall) -> dict:
        parsed_arguments = json.loads(tool_call.arguments)
        tool_call.parsed_arguments = parsed_arguments
        Formatter.validate_args(tool, tool_call)
        Formatter.convert_args(tool, tool_call)
        return tool_call

    def get_system_message(self):
        self.system_message
