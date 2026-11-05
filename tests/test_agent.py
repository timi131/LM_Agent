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


def test_promise_without_action_is_nudged(tmp_path):
    from lm_agent.agent import NUDGE

    agent = make_agent([
        answer("Данные посчитаны. Теперь я создам документ Word с этим отчётом."),
        tool_call("calculate", expression="2+2"),
        answer("Готово: 4"),
    ], tmp_path)
    assert agent.ask("сделай отчёт") == "Готово: 4"
    assert [m["content"] for m in agent.messages if m["role"] == "user"] == ["сделай отчёт", NUDGE]

    agent = make_agent([answer("Сейчас найду."), answer("Сейчас найду ещё раз."), answer("Сейчас найду в третий раз.")], tmp_path)
    assert agent.ask("x") == "Сейчас найду в третий раз."  # больше двух раз не подталкиваем — без зацикливания
    agent = make_agent([answer("Привет! Чем помочь?")], tmp_path)
    assert agent.ask("привет") == "Привет! Чем помочь?"  # обычный ответ не трогаем


def test_more_promises_nudged_and_logged(tmp_path):
    from lm_agent.agent import NUDGE

    events = []
    agent = make_agent([
        answer("Я найду информацию о населении. Пожалуйста, подождите."),
        answer("Секундочку."),
        answer("Население — 1,3 млн"),
    ], tmp_path)
    agent.log = lambda event, data: events.append(event)
    assert agent.ask("сколько жителей?") == "Население — 1,3 млн"
    assert [m["content"] for m in agent.messages if m["role"] == "user"].count(NUDGE) == 2
    assert events == ["user", "nudge", "nudge", "answer"] and agent.stop_reason == "answer"


def test_try_again_promise_nudged(tmp_path):
    agent = make_agent([answer("Извините, возникла ошибка. Давайте попробуем ещё раз."), answer("Готово")], tmp_path)
    assert agent.ask("x") == "Готово"


def test_finished_answers_are_not_promises():
    from lm_agent.agent import is_promise

    for text in [
        "Проверьте ИНН на сайте. Если ИП зарегистрировали недавно, подождите до следующего дня. Если не помогло, напишите нам.",
        "Если ИП зарегистрировали недавно, подождите до следующего дня.",
        "Откройте раздел «Имена». Затем нажмите «Сохранить».",
        "Далее введите код из SMS.",
        "Я создал файл отчёт.docx.",
        "Теперь создан отчёт за сентябрь.",
        "Сначала я проверю баланс.\n\nБаланс: 100 рублей.",
        "Подробности: https://docs.redsms.ru/knowledge-base/reports/",
    ]:
        assert not is_promise(text), text


def test_real_promises_are_detected():
    from lm_agent.agent import is_promise

    for text in [
        "Теперь я создам документ.",
        "Сейчас проверю",
        "Давайте попробуем ещё раз.",
        "Я найду информацию. Пожалуйста, подождите.",
        "Подождите немного…",
        "Секундочку.",
    ]:
        assert is_promise(text), text


def test_knowledge_attached_before_question_and_replaced(tmp_path):
    from lm_agent.agent import KB_PREFIX

    agent = make_agent([answer("Картой"), answer("Не знаю")], tmp_path)
    agent.retriever = lambda q: "Вопрос: Как оплатить?\nОтвет: Картой." if "оплат" in q else ""
    assert agent.ask("как оплатить?") == "Картой"
    assert agent.messages[1]["content"] == KB_PREFIX + "Вопрос: Как оплатить?\nОтвет: Картой."  # перед вопросом
    assert agent.messages[2]["content"] == "как оплатить?"
    assert sum(m["role"] == "system" for m in agent.messages) == 1  # системный текст не меняется: кэш Ollama цел
    agent.ask("а погода?")  # статьи к прошлому вопросу убраны, новых нет
    assert not any(m["content"].startswith(KB_PREFIX) for m in agent.messages)


def test_retriever_failure_does_not_break_answer(tmp_path):
    agent = make_agent([answer("Ок")], tmp_path)
    def boom(q):
        raise ConnectionError("нет Ollama")
    agent.retriever = boom
    assert agent.ask("вопрос") == "Ок"
