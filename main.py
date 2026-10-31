import asyncio
import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import Image

from .app.adapter import SNAPSHOT_KEY, TOOL_NAME, CapabilityBackend, build_backend, resolve_actions
from .app.background import BackgroundTasks, FollowupInbox
from .app.comment_submit import CommentSubmitter, validate_comment
from .app.event_ledger import EventLedger
from .app.execution import IndependentExecution
from .app.notification_poll import NotificationPoller, fetch_notifications, record_dump
from .app.note_store import NoteStore, WorkspaceAccess, validate_content, validate_mode, validate_note_path
from .app.post_submit import PostSubmitter, validate_post
from .app.publish import (
    COMMENT_RULES, DELETE_RULES, PREPARE_RULES, build_comment_goal, build_goal, validate_publish,
)
from .app.redaction import sanitize_text
from .app.subagent import SANDBOX_WORKER_PROTOCOL, SandboxAgent, request_stop
from .app.task_store import TaskStore
from .app.task_view import TASK_ID, read_task_evidence, refresh_task_receipts
from .app.artifacts import Artifacts, ArtifactTool, capture
from .app.work_context import WorkContext

BACKGROUND_CAPABILITIES = frozenset({
    'phone_agent', 'sandbox_worker', 'post_publish', 'post_delete', 'comment_publish',
})
# A finished round may be continued, but never one whose external effect is
# unknown: resuming would invite the executor to redo it.
CONTINUABLE_STATUSES = frozenset({
    'completed', 'needs_parent', 'step_limit', 'time_budget', 'stopped',
})


class OperationTool(FunctionTool):
    def __init__(self, owner):
        super().__init__(
            name=TOOL_NAME,
            description=(
                'Two persona-free helpers run off this one tool. Neither writes, polishes or completes anything '
                'on your behalf. Raw commands and screen dumps stay on disk; what comes back is a short summary '
                'and an evidence path. '
                'phone_agent drives the Android phone through adb: read the screen tree, tap, swipe, type, open '
                'the app. Omit actions and this is what you get. It runs in the background; its result is kept, '
                'not pushed into this conversation, so read it with operation_context_task when you want it. '
                'Its commands are screened first; clearing '
                'app data, changing settings, installing packages, rebooting and non-input broadcasts are refused. '
                'sandbox_worker works only inside the Linux workspace: files, notes, diary, scripts, calculation. '
                'It also runs in the background. Describe the job in plain language. It never touches the phone. '
                'Put any text that must be stored or typed byte for byte in input_text; neither helper may rewrite '
                'it, and neither may touch QQ. '
                'Single-purpose actions: phone_observe and phone_events look at the screen and read the reply '
                'ledger; phone_tap, phone_swipe, phone_back, phone_home, phone_start_app and phone_input take one '
                'step each; sandbox_status, phone_status and phone_app_scope are cheap state checks. '
                'post_publish takes the title in post_title and the body in post_text: the helper fills the '
                'editor, then the host verifies and submits. comment_publish takes the body in input_text and an '
                'optional nickname in post_mention: the helper fills the comment box, then the host verifies and '
                'sends. Both start in the background; their results are kept, so read them with '
                'operation_context_task. post_delete removes one of your own posts '
                'by exact title and also runs in the background; post_submit commits an editor you filled by hand. '
                'note_read and note_write read or save one note or diary file named by note_path. '
                'phone_events, note_read, note_write and anything that can be seen outside this conversation '
                'must be the only action in its call. phone_observe can share a call with navigation actions, '
                'but not with phone_events. '
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
                    'task_id': {'type': 'string', 'maxLength': 64, 'description': 'Optional. Continue that finished background task from its saved work history instead of starting blind.'},
                },
                'required': ['goal'], 'additionalProperties': False,
            },
        )
        self.owner = owner

    async def call(self, context, goal: str, actions: list[str] | None = None, input_text: str | None = None, expect_text: str | None = None, post_title: str | None = None, post_text: str | None = None, post_images: list[str] | None = None, post_mention: str | None = None, note_path: str | None = None, note_mode: str | None = None, task_id: str | None = None) -> str:
        return await self.owner.execute(context, goal, actions, input_text, expect_text, post_title, post_text, post_images, post_mention, note_path, note_mode, task_id)


class TaskTool(FunctionTool):
    def __init__(self, owner):
        super().__init__(
            name='operation_context_task',
            description=(
                'Check, stop, supplement or read background work started by operation_context_run. '
                'phone_agent, sandbox_worker, post_publish, comment_publish and post_delete start in the '
                'background and return a task_id at once; nothing is pushed into this conversation when they '
                'end, so read the result when you want it. '
                'status lists what is running here; stop ends one task and keeps its partial result; '
                'say adds one requirement to a task that is still running; result returns the saved '
                'summary and evidence path.'
            ),
            parameters={
                'type': 'object',
                'properties': {
                    'action': {'type': 'string', 'enum': ['status', 'stop', 'say', 'result']},
                    'task_id': {'type': 'string', 'maxLength': 64},
                    'text': {'type': 'string', 'maxLength': 1000, 'description': 'For say: the added requirement, in plain language. It is accepted at once and consumed before the next round.'},
                },
                'required': ['action'], 'additionalProperties': False,
            },
        )
        self.owner = owner

    async def call(self, context, action: str, task_id: str | None = None, text: str | None = None) -> str:
        return await self.owner.manage_tasks(context, action, task_id, text)


class CaptureTool(FunctionTool):
    def __init__(self, owner):
        super().__init__(
            name='operation_context_image',
            description=(
                'Capture the current phone screen or Linux desktop as a PNG artifact, '
                'view a saved artifact, or send it to this conversation. '
                'capture and view return the actual image to you, not to the user. '
                'send sends only the original image, with no generated caption. '
                'Use send only when you intend to share that image with the user. '
                'The executor does not receive images or gain a vision channel.'
            ),
            parameters={'type': 'object', 'properties': {
                'action': {'type': 'string', 'enum': ['capture', 'view', 'send']},
                'source': {'type': 'string', 'enum': ['phone', 'sandbox']},
                'artifact_id': {'type': 'string', 'maxLength': 32},
            }, 'required': ['action'], 'additionalProperties': False},
        )
        self.owner = owner

    async def call(self, context, action: str, source: str | None = None,
                   artifact_id: str | None = None):
        return await self.owner.manage_image(context, action, source, artifact_id)


class TaskRequest:
    __slots__ = (
        'run_context', 'shell', 'goal', 'permitted', 'input_text', 'expect_text',
        'post_title', 'post_text', 'post_images', 'post_mention', 'note_path', 'note_mode',
        'time_budget', 'task_id', 'resume', 'consumed_followups',
    )

    def __init__(self, *, run_context, shell, goal, permitted, input_text, expect_text,
                 post_title, post_text, post_images, post_mention, note_path, note_mode,
                 time_budget, task_id=None, resume=None):
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
        self.task_id = task_id
        self.resume = resume
        self.consumed_followups: list[str] = []


@register('astrbot_plugin_operation_context', 'Moebius', '无人格的受限沙箱与手机执行接口', '0.2.0')
class OperationContextPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir('astrbot_plugin_operation_context')
        self.executor = IndependentExecution(
            self.data_dir / 'tasks', sanitize_text,
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
        self.workspace_access = WorkspaceAccess()
        self.tasks = TaskStore(self.data_dir / 'task_state')
        self.background = BackgroundTasks()
        self.followups = FollowupInbox()
        self.artifacts = Artifacts(self.data_dir / 'artifacts')
        self.context.add_llm_tools(OperationTool(self), TaskTool(self), CaptureTool(self))

    async def initialize(self) -> None:
        prefix = '/astrbot_plugin_operation_context/api'
        self.context.register_web_api(prefix + '/tasks', self.web_tasks, ['GET'], 'Task records')
        self.context.register_web_api(prefix + '/detail', self.web_detail, ['POST'], 'Task evidence')
        self.context.register_web_api(prefix + '/control', self.web_control, ['POST'], 'Task control')
        interrupted = await self.tasks.interrupt_running()
        if interrupted:
            logger.info('Marked %s unfinished task(s) interrupted; they will not replay.', len(interrupted))
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
        await self.background.shutdown()
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
        conversation = getattr(req, 'conversation', None)
        conversation_id = getattr(conversation, 'cid', None)
        if conversation_id:
            req.contexts = await refresh_task_receipts(
                req.contexts, self.tasks, event.unified_msg_origin, conversation_id,
            )

    async def manage_image(self, run_context, action, source=None, artifact_id=None):
        event = run_context.context.event
        umo = event.unified_msg_origin
        conversation_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
        owns_phone = False
        try:
            if not event.is_admin():
                raise ValueError('admin_required')
            if action == 'capture':
                if artifact_id is not None or source not in ('phone', 'sandbox'):
                    raise ValueError('invalid_capture_request')
                if source == 'phone':
                    if self.phone_busy:
                        return json.dumps({'status': 'busy', 'reason': 'another_phone_task_running'})
                    self.phone_busy = owns_phone = True
                shell = build_backend(run_context, self.context, event.get_extra(SNAPSHOT_KEY))
                raw = await capture(shell, run_context, source)
                artifact_id = self.artifacts.save_png(raw, umo, conversation_id, source)
            elif action not in ('view', 'send') or source is not None:
                raise ValueError('invalid_artifact_action')
            path = self.artifacts.get(artifact_id, umo, conversation_id)
            if action == 'send':
                await event.send(event.chain_result([Image.fromFileSystem(str(path))]))
                return json.dumps({'status': 'sent', 'artifact_id': artifact_id})
            return ArtifactTool.image_result(artifact_id, path, run_context)
        except (ValueError, OSError, TimeoutError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            known = {'admin_required', 'invalid_capture_request', 'invalid_capture_source',
                     'invalid_artifact_action', 'unknown_artifact', 'image_too_large',
                     'invalid_image', 'capture_failed', 'phone_outside_allowed_app',
                     'desktop_not_running', 'desktop_capture_tool_missing'}
            return json.dumps({'status': 'failed', 'reason': reason if reason in known else 'image_unavailable'})
        finally:
            if owns_phone:
                self.phone_busy = False

    async def web_tasks(self):
        from quart import jsonify
        return jsonify({'tasks': await self.tasks.list_all()})

    async def web_detail(self):
        from quart import jsonify, request
        payload = await request.get_json()
        task_id = payload.get('task_id') if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            return jsonify({'error': 'unknown_task'}), 400
        record = await self.tasks.get(task_id)
        if record is None:
            return jsonify({'error': 'unknown_task'}), 404
        return jsonify(read_task_evidence(self.data_dir, record))

    async def web_control(self):
        from quart import jsonify, request
        payload = await request.get_json()
        task_id = payload.get('task_id') if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            return jsonify({'error': 'unknown_task'}), 400
        record = await self.tasks.get(task_id)
        if record is None or record.get('status') not in ('queued', 'running'):
            return jsonify({'error': 'task_not_running'}), 409
        action = payload.get('action')
        if action == 'stop':
            return jsonify({'status': 'stopping' if self.background.cancel(task_id) else 'not_running'})
        text = payload.get('text')
        if action != 'say' or not isinstance(text, str) or not text.strip() or len(text) > 1000:
            return jsonify({'error': 'invalid_followup'}), 400
        if self.background.get(task_id) is None:
            return jsonify({'error': 'task_not_running'}), 409
        self.followups.push(task_id, text.strip())
        accepted = list(record.get('followups') or []) + [text.strip()]
        await self.tasks.update(task_id, followups=accepted)
        return jsonify({'status': 'accepted'})

    @filter.command('op_stop')
    async def stop_operation(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result('只有管理员可以停止手机任务。')
            return
        request_stop()
        umo = event.unified_msg_origin
        stopped = []
        for task_id in self.background.ids():
            record = await self.tasks.get(task_id)
            if record is not None and record.get('umo') == umo and self.background.cancel(task_id):
                stopped.append(task_id)
        if stopped:
            yield event.plain_result(f'已请求停止 {len(stopped)} 个后台任务，结果会再报回来。')
            return
        yield event.plain_result('已请求停止，执行器会在下一轮开始时收手，把已完成的部分交回来。')

    async def execute(self, run_context, goal: str, actions: list[str] | None = None, input_text: str | None = None, expect_text: str | None = None, post_title: str | None = None, post_text: str | None = None, post_images: list[str] | None = None, post_mention: str | None = None, note_path: str | None = None, note_mode: str | None = None, task_id: str | None = None) -> str:
        owns_phone = False
        try:
            if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
                raise ValueError('invalid_goal')
            permitted, outward = resolve_actions(actions, self.config.get('allowed_capabilities', []))
            resume = None
            if task_id:
                if outward not in BACKGROUND_CAPABILITIES:
                    raise ValueError('continuation_requires_background_capability')
                umo = run_context.context.event.unified_msg_origin
                record = await self.tasks.get(task_id)
                if record is None or record.get('umo') != umo:
                    raise ValueError('unknown_task')
                if record.get('status') not in CONTINUABLE_STATUSES:
                    raise ValueError('task_not_continuable')
                resume = WorkContext(self.data_dir / 'work_contexts', task_id).load()
                if not resume:
                    raise ValueError('no_work_context')
                goal = (
                    f'This continues an earlier round that ended with status {record.get("status")}. '
                    'Its record is above. Do not repeat any action whose result was not confirmed.\n' + goal
                )
            needs_phone = bool(set(permitted) - {
                'sandbox_status', 'phone_events', 'note_read', 'note_write', 'sandbox_worker',
            })
            if needs_phone:
                if self.phone_busy:
                    return json.dumps({
                        'status': 'busy', 'reason': 'another_phone_task_running',
                        'needs_parent': True, 'external_action_status': 'not_attempted',
                    })
                self.phone_busy = owns_phone = True
            if outward in BACKGROUND_CAPABILITIES:
                self._validate_outward(
                    outward, input_text, expect_text, post_title, post_text,
                    post_images, post_mention, note_path, note_mode,
                )
                reply = await self._dispatch_background(
                    run_context, goal, permitted, outward, input_text, expect_text,
                    post_title, post_text, post_images, post_mention,
                    resume=resume, parent_task_id=task_id,
                )
                # The handle that runs the work owns the phone from here on.
                owns_phone = False
                return reply
            return await self._dispatch(
                run_context, goal, permitted, outward, input_text, expect_text,
                post_title, post_text, post_images, post_mention, note_path, note_mode,
            )
        except ValueError as exc:
            reason = str(exc).partition(':')[0]
            known = {
                'invalid_goal', 'actions_must_be_nonempty_list_up_to_four',
                'invalid_capability_configuration', 'capability_not_authorized',
                'duplicate_action', 'standalone_capability_must_be_alone',
                'sandbox_runtime_required', 'shipyard_neo_required',
                'parent_shell_permission_required', 'trusted_shell_backend_unavailable',
                'unexpected_note_fields', 'unexpected_text_fields', 'invalid_input_text',
                'invalid_post_title', 'invalid_post_text', 'invalid_post_images',
                'invalid_post_image_path', 'invalid_post_mention', 'post_images_disabled',
                'invalid_post_payload', 'invalid_comment_text', 'ledger_disabled',
                'unexpected_input_text', 'unexpected_post_fields', 'input_text_required',
                'invalid_expect_text', 'invalid_note_path', 'note_path_outside_allowed_dirs',
                'invalid_note_mode', 'invalid_note_content', 'note_read_failed', 'note_write_failed',
                'invalid_capture_output', 'phone_capture_failed', 'invalid_shell_envelope',
                'shell_execution_refused', 'continuation_requires_background_capability',
                'unknown_task', 'task_not_continuable', 'no_work_context',
            }
            if reason not in known:
                reason = 'invalid_request'
            return json.dumps({
                'status': 'rejected', 'needs_parent': True, 'reason': reason,
                'error_type': type(exc).__name__, 'external_action_status': 'unknown',
            })
        except TimeoutError:
            return json.dumps({
                'status': 'failed', 'needs_parent': True, 'reason': 'operation_timed_out',
                'error_type': 'TimeoutError', 'external_action_status': 'unknown',
            })
        except Exception as exc:
            logger.warning('Operation failed: %s', type(exc).__name__)
            return json.dumps({
                'status': 'failed', 'needs_parent': True, 'reason': 'execution_failed',
                'error_type': type(exc).__name__, 'external_action_status': 'unknown',
            })
        finally:
            if owns_phone:
                self.phone_busy = False

    async def _dispatch(self, run_context, goal, permitted, outward, input_text, expect_text, post_title, post_text, post_images, post_mention, note_path, note_mode) -> str:
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
        deadline = asyncio.get_running_loop().time() + time_budget
        async with asyncio.timeout(time_budget):
            if outward == 'sandbox_worker':
                async with self.workspace_access.worker():
                    request.time_budget = deadline - asyncio.get_running_loop().time()
                    return await self.standalone_handlers[outward](request)
            if outward in ('note_read', 'note_write'):
                async with self.workspace_access.note(note_path):
                    return await self.standalone_handlers[outward](request)
            if outward is not None:
                return await self.standalone_handlers[outward](request)
            if note_path is not None or note_mode is not None:
                raise ValueError('unexpected_note_fields')
            return await self.run_fixed(request)

    def background_budget(self) -> float:
        limit = self.config.get('background_task_timeout', 900)
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
            return 900.0
        return float(min(max(60, limit), 3600))

    def _validate_outward(self, outward, input_text, expect_text, post_title,
                          post_text, post_images, post_mention, note_path, note_mode) -> None:
        """Refuse bad requests before work is handed to the background.

        The same checks stay in the handlers; here they only spare the user a
        task id for something that could never run.
        """
        if outward == 'phone_agent':
            if any(value is not None for value in (expect_text, post_title, post_text)):
                raise ValueError('unexpected_text_fields')
            if input_text is not None and (
                not isinstance(input_text, str) or not input_text.strip() or len(input_text) > 2000
            ):
                raise ValueError('invalid_input_text')
        elif outward == 'sandbox_worker':
            if any(value is not None for value in (
                expect_text, post_title, post_text, post_images, post_mention,
                note_path, note_mode,
            )):
                raise ValueError('unexpected_text_fields')
            if input_text is not None and (
                not isinstance(input_text, str) or not input_text.strip()
                or len(input_text) > 2000
            ):
                raise ValueError('invalid_input_text')
        elif outward == 'post_publish':
            if input_text is not None or expect_text is not None:
                raise ValueError('unexpected_text_fields')
            validate_publish(post_title, post_text, post_images, post_mention)
        elif outward == 'comment_publish':
            if any(value is not None for value in (
                expect_text, post_title, post_text, post_images,
            )):
                raise ValueError('unexpected_text_fields')
            validate_comment(input_text)
        elif outward == 'post_delete':
            if any(value is not None for value in (input_text, expect_text, post_text)):
                raise ValueError('unexpected_text_fields')
            if not isinstance(post_title, str) or not post_title.strip() or len(post_title) > 100:
                raise ValueError('invalid_post_title')

    async def _dispatch_background(self, run_context, goal, permitted, outward,
                                   input_text, expect_text, post_title, post_text,
                                   post_images, post_mention, resume=None,
                                   parent_task_id=None) -> str:
        event = run_context.context.event
        umo = event.unified_msg_origin
        conversation_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
        shell = build_backend(run_context, self.context, event.get_extra(SNAPSHOT_KEY))
        record = await self.tasks.create(
            umo=umo, conversation_id=conversation_id,
            kind=outward, description=goal,
        )
        request = TaskRequest(
            run_context=run_context, shell=shell,
            goal=goal, permitted=permitted, input_text=input_text, expect_text=expect_text,
            post_title=post_title, post_text=post_text, post_images=post_images,
            post_mention=post_mention, note_path=None, note_mode=None,
            time_budget=self.background_budget(), task_id=record['task_id'], resume=resume,
        )
        if parent_task_id:
            await self.tasks.update(record['task_id'], parent_task_id=parent_task_id)
        self.background.spawn(record['task_id'], self._run_background(record, request))
        return json.dumps({
            'status': 'dispatched',
            'task_id': record['task_id'],
            'kind': outward,
            'note': 'Started in the background.',
        }, ensure_ascii=False)

    async def _run_background(self, record: dict, request: TaskRequest) -> None:
        task_id = record['task_id']
        owns_phone = record['kind'] != 'sandbox_worker'
        budget = self.background_budget()
        started = asyncio.get_running_loop().time()
        try:
            await self.tasks.update(task_id, status='running')
            async with asyncio.timeout(budget):
                handler = self.standalone_handlers[record['kind']]
                if record['kind'] == 'sandbox_worker':
                    async with self.workspace_access.worker():
                        request.time_budget = budget - (asyncio.get_running_loop().time() - started)
                        raw = await handler(request)
                else:
                    raw = await handler(request)
            result = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(result, dict):
                result = {}
            status, summary = self._background_outcome(record['kind'], result)
            await self.tasks.update(
                task_id, status=status,
                summary=summary, result=self._bounded_result(result),
            )
        except asyncio.CancelledError:
            await self.tasks.update(
                task_id, status='stopped',
                summary='Stopped at the request of the host. Look at the phone before continuing.',
            )
        except TimeoutError:
            await self.tasks.update(
                task_id, status='time_budget',
                summary='The task used its whole background budget and was stopped.',
            )
        except Exception as exc:
            logger.warning('Background task failed: %s', type(exc).__name__)
            await self.tasks.update(
                task_id, status='failed',
                summary=f'The task failed: {type(exc).__name__}',
            )
        finally:
            if owns_phone:
                self.phone_busy = False
            consumed = list(request.consumed_followups)
            if consumed:
                await self.tasks.update(task_id, followups_consumed=consumed)
            self.followups.discard(task_id)

    @staticmethod
    def _background_outcome(kind: str, result: dict) -> tuple[str, str | None]:
        """One status and one short sentence for the parent.

        A submission that was attempted but not confirmed is never reported as
        done: the host cannot tell whether it landed.
        """
        if kind not in ('post_publish', 'comment_publish'):
            return result.get('status') or 'failed', result.get('summary')
        preparation = result.get('preparation') or {}
        submit = result.get('submit') or {}
        if preparation.get('status') != 'completed':
            return 'needs_parent', preparation.get('summary')
        if submit.get('status') == 'confirmed':
            return 'completed', 'Preparation and submission were confirmed on the page.'
        if submit.get('status') == 'unknown':
            return 'needs_parent', 'Submission was attempted but not confirmed. Look at the phone before doing anything else.'
        return 'needs_parent', 'Preparation finished but nothing was submitted.'

    @staticmethod
    def _bounded_result(result: dict) -> dict:
        keys = (
            'task_id', 'status', 'reason', 'summary', 'command_count',
            'device_action_count', 'external_action_status', 'raw_record',
        )
        bounded = {key: result[key] for key in keys if key in result}
        for key in ('preparation', 'submit'):
            value = result.get(key)
            if isinstance(value, dict):
                bounded[key] = {
                    field: value[field] for field in (
                        'status', 'reason', 'summary', 'command_count',
                        'raw_record', 'object_id_verified',
                    ) if field in value
                }
        return bounded

    @staticmethod
    def _public_record(record: dict) -> dict:
        keys = ('task_id', 'kind', 'status', 'summary', 'description', 'created_at', 'artifacts')
        public = {key: record.get(key) for key in keys}
        public['followups_accepted'] = len(record.get('followups') or [])
        public['followups_consumed'] = len(record.get('followups_consumed') or [])
        if record.get('parent_task_id'):
            public['continues'] = record['parent_task_id']
        return public

    async def manage_tasks(self, run_context, action: str, task_id: str | None = None,
                           text: str | None = None) -> str:
        umo = run_context.context.event.unified_msg_origin
        if action == 'status':
            records = await self.tasks.list_for(umo)
            running = [
                self._public_record(record) for record in records
                if record.get('status') in ('queued', 'running')
            ]
            recent = [
                self._public_record(record) for record in records
                if record.get('status') not in ('queued', 'running')
            ][:3]
            return json.dumps({'running': running, 'recent': recent}, ensure_ascii=False)
        if action not in ('stop', 'result', 'say'):
            return json.dumps({'status': 'rejected', 'reason': 'invalid_task_action'})
        record = await self.tasks.get(task_id)
        if record is None or record.get('umo') != umo:
            return json.dumps({'status': 'rejected', 'reason': 'unknown_task'})
        if action == 'say':
            if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                return json.dumps({'status': 'rejected', 'reason': 'invalid_followup_text'})
            if self.background.get(task_id) is None or record.get('status') not in ('queued', 'running'):
                return json.dumps({'status': 'rejected', 'reason': 'task_not_running'})
            self.followups.push(task_id, text.strip())
            accepted = list(record.get('followups') or [])
            accepted.append(text.strip())
            await self.tasks.update(task_id, followups=accepted)
            return json.dumps({
                'status': 'accepted', 'task_id': task_id,
                'note': 'Added to the task. Acceptance is not consumption; it is read before the next round.',
            }, ensure_ascii=False)
        if action == 'stop':
            stopped = self.background.cancel(task_id)
            if not stopped:
                request_stop()
            return json.dumps({
                'status': 'stopping' if stopped else 'not_running',
                'task_id': task_id,
            }, ensure_ascii=False)
        return json.dumps({
            'status': 'ok', 'task': self._public_record(record),
            'result': record.get('result'),
        }, ensure_ascii=False)

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
        task_id = request.task_id
        work_context = None
        take_followups = None
        if task_id:
            work_context = WorkContext(self.data_dir / 'work_contexts', task_id)

            def take_followups():
                texts = self.followups.drain(task_id)
                request.consumed_followups.extend(texts)
                return texts

        async def capture_artifact():
            event = request.run_context.context.event
            umo = event.unified_msg_origin
            conversation_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if task_id:
                record = await self.tasks.get(task_id)
                conversation_id = record.get('conversation_id') if record else conversation_id
            source = 'sandbox' if evidence_dir == 'sandbox_worker' else 'phone'
            raw = await capture(request.shell, request.run_context, source)
            artifact_id = self.artifacts.save_png(raw, umo, conversation_id, source)
            if task_id:
                record = await self.tasks.get(task_id)
                saved = list((record or {}).get('artifacts') or [])
                await self.tasks.update(task_id, artifacts=saved + [artifact_id])
            return artifact_id

        return await agent.run(
            context=self.context, provider_id=await self.provider_id(request.run_context.context.event),
            goal=goal, max_steps=self.config.get('max_steps', 30),
            command_timeout=self.config.get('tool_call_timeout', 60),
            time_budget=request.time_budget, resume=request.resume,
            work_context=work_context, take_followups=take_followups,
            capture_artifact=capture_artifact, **kwargs,
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
            request, build_goal(request.goal, request.post_images, request.post_mention),
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
            request, request.goal + '\nDelete one of your own posts. Read its exact title from frozen_files.target in the task payload.',
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
            request, build_comment_goal(request.goal, request.post_mention),
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
