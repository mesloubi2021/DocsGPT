import json

from application.core.mongo_db import MongoDB
from application.llm.llm_creator import LLMCreator
from application.tools.tool_manager import ToolManager


class Agent:
    def __init__(self, llm_name, gpt_model, api_key, user_api_key=None):
        # Initialize the LLM with the provided parameters
        self.llm = LLMCreator.create_llm(
            llm_name, api_key=api_key, user_api_key=user_api_key
        )
        self.gpt_model = gpt_model
        # Static tool configuration (to be replaced later)
        self.tools = []
        self.tool_config = {}

    def _get_user_tools(self, user="local"):
        mongo = MongoDB.get_client()
        db = mongo["docsgpt"]
        user_tools_collection = db["user_tools"]
        user_tools = user_tools_collection.find({"user": user, "status": True})
        user_tools = list(user_tools)
        tools_by_id = {str(tool["_id"]): tool for tool in user_tools}
        return tools_by_id

    def _prepare_tools(self, tools_dict):
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": f"{action['name']}_{tool_id}",
                    "description": action["description"],
                    "parameters": {
                        **action["parameters"],
                        "properties": {
                            k: {
                                key: value
                                for key, value in v.items()
                                if key != "filled_by_llm" and key != "value"
                            }
                            for k, v in action["parameters"]["properties"].items()
                            if v.get("filled_by_llm", False)
                        },
                        "required": [
                            key
                            for key in action["parameters"]["required"]
                            if key in action["parameters"]["properties"]
                            and action["parameters"]["properties"][key].get(
                                "filled_by_llm", False
                            )
                        ],
                    },
                },
            }
            for tool_id, tool in tools_dict.items()
            for action in tool["actions"]
            if action["active"]
        ]

    def _execute_tool_action(self, tools_dict, call):
        call_id = call.id
        call_args = json.loads(call.function.arguments)
        tool_id = call.function.name.split("_")[-1]
        action_name = call.function.name.rsplit("_", 1)[0]

        tool_data = tools_dict[tool_id]
        action_data = next(
            action for action in tool_data["actions"] if action["name"] == action_name
        )

        for param, details in action_data["parameters"]["properties"].items():
            if param not in call_args and "value" in details:
                call_args[param] = details["value"]

        tm = ToolManager(config={})
        tool = tm.load_tool(tool_data["name"], tool_config=tool_data["config"])
        print(f"Executing tool: {action_name} with args: {call_args}")
        return tool.execute_action(action_name, **call_args), call_id

    def _simple_tool_agent(self, messages):
        tools_dict = self._get_user_tools()
        self._prepare_tools(tools_dict)

        resp = self.llm.gen(model=self.gpt_model, messages=messages, tools=self.tools)

        if isinstance(resp, str):
            yield resp
            return
        if resp.message.content:
            yield resp.message.content
            return

        while resp.finish_reason == "tool_calls":
            message = json.loads(resp.model_dump_json())["message"]
            keys_to_remove = {"audio", "function_call", "refusal"}
            filtered_data = {
                k: v for k, v in message.items() if k not in keys_to_remove
            }
            messages.append(filtered_data)
            tool_calls = resp.message.tool_calls
            for call in tool_calls:
                try:
                    tool_response, call_id = self._execute_tool_action(tools_dict, call)
                    messages.append(
                        {
                            "role": "tool",
                            "content": str(tool_response),
                            "tool_call_id": call_id,
                        }
                    )
                except Exception as e:
                    messages.append(
                        {
                            "role": "tool",
                            "content": f"Error executing tool: {str(e)}",
                            "tool_call_id": call.id,
                        }
                    )
            # Generate a new response from the LLM after processing tools
            resp = self.llm.gen(
                model=self.gpt_model, messages=messages, tools=self.tools
            )

        # If no tool calls are needed, generate the final response
        if isinstance(resp, str):
            yield resp
        elif resp.message.content:
            yield resp.message.content
        else:
            completion = self.llm.gen_stream(
                model=self.gpt_model, messages=messages, tools=self.tools
            )
            for line in completion:
                yield line

        return

    def gen(self, messages):
        # Generate initial response from the LLM
        if self.llm.supports_tools():
            resp = self._simple_tool_agent(messages)
            for line in resp:
                yield line
        else:
            resp = self.llm.gen_stream(model=self.gpt_model, messages=messages)
            for line in resp:
                yield line
