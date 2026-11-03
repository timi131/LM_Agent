from lm_agent import Agent, Settings, ToolRegistry
from lm_agent.toolkits import basic


class FakeClient:
    """Возвращает заранее заданные ответы и запоминает запросы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def tool_call(name, **arguments):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}}


def answer(text):
    return {"message": {"role": "assistant", "content": text}}


def make_agent(responses, tmp_path, context=None, **overrides):
    settings = Settings(workspace=tmp_path, **overrides)
    registry = ToolRegistry()
    basic.register(registry)
    return Agent(client=FakeClient(responses), tools=registry, settings=settings, context=context)


def test_plain_answer(tmp_path):
    agent = make_agent([answer("Привет!")], tmp_path)
    assert agent.ask("привет") == "Привет!"


def test_tool_loop(tmp_path):
    agent = make_agent([tool_call("calculate", expression="2+2*2"), answer("Будет 6")], tmp_path)
    assert agent.ask("сколько 2+2*2?") == "Будет 6"
    tool_msg = agent.messages[-2]
    assert tool_msg == {"role": "tool", "content": "6", "tool_name": "calculate"}
    assert agent.client.calls[0]["tools"]  # схемы инструментов переданы модели


def test_unknown_tool_reported_to_model(tmp_path):
    agent = make_agent([tool_call("nope"), answer("ok")], tmp_path)
    agent.ask("x")
    assert "неизвестный инструмент" in agent.messages[-2]["content"]


def test_max_steps(tmp_path):
    agent = make_agent([tool_call("get_current_time")] * 3, tmp_path, max_steps=3)
    assert "лимит шагов" in agent.ask("время?")


def test_reset_keeps_system_prompt(tmp_path):
    agent = make_agent([answer("a")], tmp_path)
    agent.ask("q")
    agent.reset()
    assert [m["role"] for m in agent.messages] == ["system"]


def test_context_goes_into_system_prompt(tmp_path):
    agent = make_agent([], tmp_path, context=lambda: "Пользователь любит краткость")
    assert agent.messages[0]["content"].endswith("Пользователь любит краткость")


def test_repeated_identical_call_is_stopped(tmp_path):
    same = tool_call("calculate", expression="1/0")
    agent = make_agent([same, same, same, answer("Сдаюсь")], tmp_path)
    assert agent.ask("посчитай") == "Сдаюсь"
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "Ошибка" in results[0] and "уже делал ровно этот вызов" in results[1]
    assert results[2].startswith("Не выполнено")
