import json
import anthropic
from config import ANTHROPIC_API_KEY

MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 20

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class BaseAgent:
    """Run a tool-use loop: Claude picks tools, we execute them, until it stops."""

    name: str = "base"
    system_prompt: str = ""

    def __init__(self):
        self.tools: list[dict] = []
        self._tool_handlers: dict = {}

    def register_tool(self, definition: dict, handler):
        self.tools.append(definition)
        self._tool_handlers[definition["name"]] = handler

    def run(self, task: str, context: dict = None) -> str:
        user_content = task
        if context:
            user_content = f"{task}\n\nContext:\n{json.dumps(context, indent=2)}"

        messages = [{"role": "user", "content": user_content}]

        for i in range(MAX_ITERATIONS):
            response = client.messages.create(
                model=MODEL,
                max_tokens=8096,
                system=self.system_prompt,
                tools=self.tools,
                messages=messages,
            )
            print(f"  [iter {i+1}] stop_reason={response.stop_reason}")

            if response.stop_reason == "end_turn":
                text_blocks = [b.text for b in response.content if b.type == "text"]
                return " ".join(text_blocks)

            if response.stop_reason != "tool_use":
                print(f"  [iter {i+1}] unexpected stop: {response.stop_reason}")
                break

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                print(f"  [tool] {block.name}({list(block.input.keys())})")
                handler = self._tool_handlers.get(block.name)
                if handler is None:
                    result = {"error": f"Unknown tool: {block.name}"}
                else:
                    try:
                        result = handler(**block.input)
                    except Exception as e:
                        result = {"error": str(e)}
                print(f"  [result] {str(result)[:120]}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

        return "Agent reached iteration limit without a final answer."
