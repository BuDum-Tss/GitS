"""Логика модуля: инструменты и обработчики. Запросы к ядру идут только через методы клиента SDK (в тестах их отдаёт FakeCore)."""
from gits_sdk import ModuleClient, ToolError


def build_client(manifest: dict, **kwargs) -> ModuleClient:
    """Собрать клиента и повесить обработчики инструментов из манифеста. `kwargs` (url, token) нужны тестам."""
    client = ModuleClient(manifest, **kwargs)

    @client.tool("greet")
    async def greet(args):
        who = str(args.get("name") or "").strip()
        if not who:
            raise ToolError("name не должен быть пустым", code="bad_request")
        persona = await client.persona()                # типизированный запрос core.persona: поля, а не словарь
        await client.event("template.greeted", f"Поздоровались с {who}.", data={"who": who})
        return f"{persona.name}: здравствуйте, {who}."   # факт для планировщика; готовую реплику строит ядро

    @client.tool("remember_fact")
    async def remember_fact(args):
        text = str(args.get("text") or "").strip()
        if not text:
            raise ToolError("text не должен быть пустым", code="bad_request")
        stored = await client.memory_remember(text, source=client.module_id)
        facts = await client.memory_recall(text, limit=3)
        return {"stored": stored, "found": [f.get("text", "") for f in facts]}

    @client.on_topic("presence")
    async def presence(data):
        """Тема presence: говорит ли она сейчас и прочее. Здесь можно подстроить работу модуля."""

    @client.on_reconnect
    async def reconnected():
        await client.resend_jobs()                       # MOD-22: после обрыва повторно отдать незавершённые задачи

    return client
