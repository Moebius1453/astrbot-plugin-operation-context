import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import FunctionTool

from .app.adapter import SNAPSHOT_KEY, TOOL_NAME, CapabilityBackend, build_backend, resolve_actions
from .app.comment_submit import CommentSubmitter, validate_comment
from .app.event_ledger import EventLedger
from .app.execution import IndependentExecution
from .app.notification_poll import NotificationPoller, fetch_notifications, record_dump
from .app.note_store import NoteStore, validate_content, validate_mode, validate_note_path
from .app.post_submit import PostSubmitter, validate_post
from .app.publish import (
    COMMENT_RULES, DELETE_RULES, PREPARE_RULES, build_comment_goal, build_goal, validate_publish,
)
from .app.redaction import sanitize_text
from .app.subagent import SANDBOX_WORKER_PROTOCOL, SandboxAgent, request_stop


class OperationTool(FunctionTool):
    def __init__(self, owner):
        super().__init__(
            name=TOOL_NAME,
            description=(
                'Two persona-free helpers run off this one tool. Neither writes, polishes or completes anything '
                'on your behalf. Raw commands and screen dumps stay on disk; you get one short summary and an '
                'evidence path. '
                'phone_agent drives the Android phone through adb: read the screen tree, tap, swipe, type, open '
                'the app. Omit actions and this is what you get. Its commands are screened first; clearing app '
                'data, changing settings, installing packages, rebooting and non-input broadcasts are refused. '
                'sandbox_worker works only inside the Linux workspace: files, notes, diary, scripts, calculation. '
                'Describe the job in plain language. It never touches the phone. '
                'Put any text that must be stored or typed byte for byte in input_text; neither helper may rewrite '
                'it, and neither may touch QQ. '
                'Single-purpose actions: phone_observe and phone_events look at the screen and read the reply '
                'ledger; phone_tap, phone_swipe, phone_back, phone_home, phone_start_app and phone_input take one '
                'step each; sandbox_status, phone_status and phone_app_scope are cheap state checks. '
                'post_publish takes the title in post_title and the body in post_text: the helper fills the '
                'editor, then the host verifies and submits. comment_publish takes the body in input_text and an '
                'optional nickname in post_mention: the helper fills the comment box, then the host verifies and '
                'sends. post_delete removes one of your own posts by exact title; post_submit commits an editor '
                'you filled by hand. '
                'note_read and note_write read or save one note or diary file named by note_path. '
                'Anything that can be seen by someone outside this conversation has to be used alone. '
                'Handle anything outside this scope yourself.'
            ),
            parameters={
                'type': 'object',
                'properties': {
                    'goal': {'type': 'string', 'maxLength': 4000},
                    'actions': {'type': 'array', 'items': {'type': 'string', 'enum': ['sandbox_status', 'phone_status', 'phone_app_scope', 'phone_observe', 'phone_events', 'phone_tap', 'phone_swipe', 'phone_back', 'phone_home', 'phone_start_app', 'phone_input', 'post_submit', 'post_publish', 'post_delete', 'comment_publish', 'phone_agent', 'note_read', 'note_write', 'sandbox_worker']}, 'minItems': 1, 'maxItems': 4, 'uniqueItems': True, 'description': 'Optional. Omit for phone_agent.'},
                    'input_text': {'type': 'string', 'maxLength': 2000, 'description': 'Exact text the executor may type or publish. It must not rewrite it.'},
                    'expect_text': {'type': 'string', 'maxLength': 200, 'description': 'Optional. Text you want confirmed on the final screen. Only a boolean is returned.'},
                    'post_title': {'type': 'string', 'maxLength': 100, 'description': 'Exact title for post_submit, post_publish or the post to remove for post_delete.'},
                    'post_text': {'type': 'string', 'maxLength': 4000, 'description': 'Exact post body for post_submit or post_publish.'},
                    'post_images': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 0, 'description': 'Not supported yet. Leave empty.'},
                    'post_mention': {'type': 'string', 'maxLength': 50, 'description': 'Optional nickname to mention with the native picker, for post_publish or comment_publish.'},
                    'note_path': {'type': 'string', 'maxLength': 200, 'description': 'For note_read and note_write: path relative to her workspace, such as notes/某笔记.md or diary/2026-10/2026-10-27.md.'},
                    'note_mode': {'type': 'string', 'enum': ['create', 'append', 'replace'], 'description': 'For note_write: create fails if the file exists, append adds to it, replace backs it up first.'},
                },
                'required': ['goal'], 'additionalProperties': False,
            },
        )
        self.owner = owner

    async def call(self, context, goal: str, actions: list[str] | None = None, input_text: str | None = None, expect_text: str | None = None, post_title: str | None = None, post_text: str | None = None, post_images: list[str] | None = None, post_mention: str | None = None, note_path: str | None = None, note_mode: str | None = None) -> str:
        return await self.owner.execute(context, goal, actions, input_text, expect_text, post_title, post_text, post_images, post_mention, note_path, note_mode)


class TaskRequest:
    __slots__ = (
        'run_context', 'shell', 'goal', 'permitted', 'input_text', 'expect_text',
        'post_title', 'post_text', 'post_images', 'post_mention', 'note_path', 'note_mode',
        'time_budget',
    )

    def __init__(self, *, run_context, shell, goal, permitted, input_text, expect_text,
                 post_title, post_text, post_images, post_mention, note_path, note_mode,
                 time_budget):
        self.run_context = run_context
        self.shell = shell
        self.goal = goal
        self.permitted = permitted
        self.input_text = input_text
        self.expect_text = expect_text
        self.post_title = post_title
        self.post_text = post_text
        self.post_images = post_images
        self.post_mention = post_mention
        self.note_path = note_path
        self.note_mode = note_mode
        self.time_budget = time_budget


@register('astrbot_plugin_operation_context', 'Moebius', '无人格的受限沙箱与手机执行接口', '0.2.0')
class OperationContextPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.executor = IndependentExecution(
            StarTools.get_data_dir('astrbot_plugin_operation_context') / 'tasks', sanitize_text,
        )
        self.standalone_handlers = {
            'phone_agent': self.handle_agent,
            'post_publish': self.handle_publish,
            'post_delete': self.handle_delete,
            'post_submit': self.handle_submit,
            'comment_publish': self.handle_comment,
            'phone_events': self.handle_events,
            'note_read': self.handle_note_read,
            'note_write': self.handle_note_write,
            'sandbox_worker': self.handle_worker,
        }
        self.ledger = None
        self.poller = None
        self.phone_busy = False
        self.context.add_llm_tools(OperationTool(self))

    async def initialize(self) -> None:
        if not self.config.get('ledger_enabled'):
            return
        data_dir = StarTools.get_data_dir('astrbot_plugin_operation_context')
        self.ledger = EventLedger(data_dir / 'ledger.db')
        self.poller = NotificationPoller(
            lambda: fetch_notifications(self.context),
            lambda dump: record_dump(self.ledger, dump),
            self.config.get('poll_interval_seconds', 600),
        )
        self.poller.start()
        logger.info('Notification ledger polling every %s seconds', self.poller.interval)

    async def terminate(self) -> None:
        if self.poller is not None:
            await self.poller.stop()
            self.poller = None
        if self.ledger is not None:
            self.ledger.close()
            self.ledger = None

    @filter.on_llm_request(priority=-10000)
    async def capture_authorized_tools(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        tools = req.func_tool
        event.set_extra(SNAPSHOT_KEY, [tool.name for tool in tools.tools if tool.active] if tools else [])

    @filter.command('op_stop')
    async def stop_operation(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result('只有管理员可以停止手机任务。')
            return
        request_stop()
        yield event.plain_result('已请求停止，执行器会在下一轮开始时收手，把已完成的部分交回来。')

    async def execute(self, run_context, goal: str, actions: list[str] | None = None, input_text: str | None = None, expect_text: str | None = None, post_title: str | None = None, post_text: str | None = None, post_images: list[str] | None = None, post_mention: str | None = None, note_path: str | None = None, note_mode: str | None = None) -> str:
        if self.phone_busy:
            return json.dumps({
                'status': 'busy', 'reason': 'another_phone_task_running',
                'needs_parent': True, 'external_action_status': 'not_attempted',
            })
        self.phone_busy = True
        try:
            return await self._dispatch(
                run_context, goal, actions, input_text, expect_text,
                post_title, post_text, post_images, post_mention, note_path, note_mode,
            )
        finally:
            self.phone_busy = False

    async def _dispatch(self, run_context, goal, actions, input_text, expect_text, post_title, post_text, post_images, post_mention, note_path, note_mode) -> str:
        try:
            permitted, outward = resolve_actions(actions, self.config.get('allowed_capabilities', []))
            event = run_context.context.event
            settings = self.context.get_config(umo=event.unified_msg_origin).get('provider_settings', {})
            limit = settings.get('tool_call_timeout')
            time_budget = max(30, float(limit) - 10) if isinstance(limit, (int, float)) else 110.0
            request = TaskRequest(
                run_context=run_context, shell=build_backend(run_context, self.context, event.get_extra(SNAPSHOT_KEY)),
                goal=goal, permitted=permitted, input_text=input_text, expect_text=expect_text,
                post_title=post_title, post_text=post_text, post_images=post_images, post_mention=post_mention,
                note_path=note_path, note_mode=note_mode,
                time_budget=time_budget,
            )
            if outward is not None:
                return await self.standalone_handlers[outward](request)
            if note_path is not None or note_mode is not None:
                raise ValueError('unexpected_note_fields')
            return await self.run_fixed(request)
        except Exception as exc:
            logger.warning('Restricted operation rejected or failed: %s', type(exc).__name__)
            return json.dumps({'status': 'rejected', 'needs_parent': True, 'error_type': type(exc).__name__, 'external_action_status': 'not_attempted'})

    async def provider_id(self, event) -> str:
        configured = self.config.get('executor_provider')
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return await self.context.get_current_chat_provider_id(event.unified_msg_origin)

    async def run_agent(self, request: TaskRequest, goal: str, evidence_dir: str = 'phone_agent', **kwargs) -> dict:
        agent = SandboxAgent(
            request.shell, request.run_context,
            StarTools.get_data_dir('astrbot_plugin_operation_context') / evidence_dir,
        )
        return await agent.run(
            context=self.context, provider_id=await self.provider_id(request.run_context.context.event),
            goal=goal, max_steps=self.config.get('max_steps', 30),
            command_timeout=self.config.get('tool_call_timeout', 60),
            time_budget=request.time_budget, **kwargs,
        )

    async def submitter(self, request: TaskRequest) -> PostSubmitter:
        async def execute_command(command):
            return json.loads(await request.shell.call(
                request.run_context, command=command, background=False, timeout=120,
            ))
        return PostSubmitter(
            execute_command,
            StarTools.get_data_dir('astrbot_plugin_operation_context') / 'post_submissions',
        )

    async def comment_submitter(self, request: TaskRequest) -> CommentSubmitter:
        async def execute_command(command):
            return json.loads(await request.shell.call(
                request.run_context, command=command, background=False, timeout=120,
            ))
        return CommentSubmitter(
            execute_command,
            StarTools.get_data_dir('astrbot_plugin_operation_context') / 'comment_submissions',
        )

    async def handle_agent(self, request: TaskRequest) -> str:
        if any(value is not None for value in (request.expect_text, request.post_title, request.post_text)):
            raise ValueError('unexpected_text_fields')
        if request.input_text is not None and (
            not isinstance(request.input_text, str) or not request.input_text.strip() or len(request.input_text) > 2000
        ):
            raise ValueError('invalid_input_text')
        result = await self.run_agent(request, request.goal, frozen_text=request.input_text)
        return json.dumps(result, ensure_ascii=False)

    async def handle_publish(self, request: TaskRequest) -> str:
        if request.input_text is not None or request.expect_text is not None:
            raise ValueError('unexpected_text_fields')
        validate_publish(request.post_title, request.post_text, request.post_images, request.post_mention)
        preparation = await self.run_agent(
            request, build_goal(request.post_images, request.post_mention),
            frozen_files={'title': request.post_title, 'body': request.post_text},
            extra_rules=PREPARE_RULES,
        )
        result = {
            'preparation': {
                'status': preparation['status'], 'summary': preparation['summary'],
                'command_count': preparation['command_count'],
                'raw_record': preparation['raw_record'],
            },
            'submit': {'status': 'skipped', 'reason': 'preparation_not_completed'},
        }
        if preparation['status'] == 'completed':
            submitter = await self.submitter(request)
            result['submit'] = await submitter.submit(request.post_title, request.post_text)
        return json.dumps(result, ensure_ascii=False)

    async def handle_delete(self, request: TaskRequest) -> str:
        if any(value is not None for value in (request.input_text, request.expect_text, request.post_text)):
            raise ValueError('unexpected_text_fields')
        if not isinstance(request.post_title, str) or not request.post_title.strip() or len(request.post_title) > 100:
            raise ValueError('invalid_post_title')
        result = await self.run_agent(
            request, 'Delete one of your own posts. The exact title is in /tmp/operation-target.txt.',
            frozen_files={'target': request.post_title}, extra_rules=DELETE_RULES,
        )
        return json.dumps(result, ensure_ascii=False)

    async def handle_submit(self, request: TaskRequest) -> str:
        if request.input_text is not None or request.expect_text is not None:
            raise ValueError('unexpected_text_fields')
        validate_post(request.post_title, request.post_text)
        submitter = await self.submitter(request)
        return json.dumps(await submitter.submit(request.post_title, request.post_text), ensure_ascii=False)

    async def handle_comment(self, request: TaskRequest) -> str:
        if any(value is not None for value in (
            request.expect_text, request.post_title, request.post_text, request.post_images,
        )):
            raise ValueError('unexpected_text_fields')
        validate_comment(request.input_text)
        preparation = await self.run_agent(
            request, build_comment_goal(request.post_mention),
            frozen_text=request.input_text, extra_rules=COMMENT_RULES,
        )
        result = {
            'preparation': {
                'status': preparation['status'], 'reason': preparation.get('reason'),
                'summary': preparation['summary'], 'command_count': preparation['command_count'],
                'raw_record': preparation['raw_record'],
            },
            'submit': {'status': 'skipped', 'reason': 'preparation_not_completed'},
        }
        if preparation['status'] == 'completed':
            submitter = await self.comment_submitter(request)
            result['submit'] = await submitter.submit(request.input_text)
        return json.dumps(result, ensure_ascii=False)

    async def handle_events(self, request: TaskRequest) -> str:
        if any(value is not None for value in (
            request.input_text, request.expect_text, request.post_title,
            request.post_text, request.post_mention,
        )):
            raise ValueError('unexpected_text_fields')
        if self.ledger is None:
            raise ValueError('ledger_disabled')
        events = self.ledger.pending(limit=20)
        actors = {
            event['author']: self.ledger.actor(event['author'])
            for event in events if event['author']
        }
        return json.dumps({
            'events': events, 'actors': actors, 'counts': self.ledger.counts(),
        }, ensure_ascii=False)

    async def handle_worker(self, request: TaskRequest) -> str:
        if any(value is not None for value in (
            request.expect_text, request.post_title, request.post_text, request.post_images,
            request.post_mention, request.note_path, request.note_mode,
        )):
            raise ValueError('unexpected_text_fields')
        if request.input_text is not None and (
            not isinstance(request.input_text, str) or not request.input_text.strip()
            or len(request.input_text) > 2000
        ):
            raise ValueError('invalid_input_text')
        result = await self.run_agent(
            request, request.goal, evidence_dir='sandbox_worker', frozen_text=request.input_text,
            protocol=SANDBOX_WORKER_PROTOCOL, needs_device_action=False,
        )
        return json.dumps(result, ensure_ascii=False)

    async def note_store(self, request: TaskRequest) -> NoteStore:
        async def execute_command(command):
            return json.loads(await request.shell.call(
                request.run_context, command=command, background=False, timeout=60,
            ))
        return NoteStore(execute_command)

    async def handle_note_read(self, request: TaskRequest) -> str:
        if any(value is not None for value in (
            request.input_text, request.expect_text, request.post_title, request.post_text,
            request.post_images, request.post_mention, request.note_mode,
        )):
            raise ValueError('unexpected_text_fields')
        store = await self.note_store(request)
        return json.dumps(await store.read(request.note_path), ensure_ascii=False)

    async def handle_note_write(self, request: TaskRequest) -> str:
        if any(value is not None for value in (
            request.expect_text, request.post_title, request.post_text,
            request.post_images, request.post_mention,
        )):
            raise ValueError('unexpected_text_fields')
        validate_note_path(request.note_path)
        validate_mode(request.note_mode)
        validate_content(request.input_text)
        store = await self.note_store(request)
        result = await store.write(request.note_path, request.note_mode, request.input_text)
        return json.dumps(result, ensure_ascii=False)

    async def run_fixed(self, request: TaskRequest) -> str:
        if request.post_title is not None or request.post_text is not None or request.post_mention not in (None, ''):
            raise ValueError('unexpected_post_fields')
        frozen = {}
        if 'phone_input' in request.permitted:
            if not isinstance(request.input_text, str) or not request.input_text.strip() or len(request.input_text) > 2000:
                raise ValueError('input_text_required')
            frozen['text'] = request.input_text
        elif request.input_text is not None:
            raise ValueError('unexpected_input_text')
        if request.expect_text is not None:
            if not isinstance(request.expect_text, str) or not request.expect_text.strip() or len(request.expect_text) > 200:
                raise ValueError('invalid_expect_text')
            frozen['expect_text'] = request.expect_text
        result = await self.executor.run(
            context=self.context, provider_id=await self.provider_id(request.run_context.context.event),
            goal=request.goal, actions=request.permitted,
            backend=CapabilityBackend(request.shell, request.run_context, frozen),
            max_steps=self.config.get('max_steps', 4),
            tool_call_timeout=self.config.get('tool_call_timeout', 30),
            frozen=frozen, time_budget=request.time_budget,
        )
        return json.dumps(result, ensure_ascii=False)
