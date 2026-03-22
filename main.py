import time
import asyncio
import re

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType

@register("astrbot_plugin_bye", "largefox", "识别不友善以及不欢迎bot的群聊，让bot主动退群，保护bot身心健康，从源头上节省Tokens。", "1.0.0")
class ByePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.mute_stats = {}      
        self.pending_leaves = {}  
        self.hostile_stats = {}   
        self._background_tasks = set()  

    def _create_task(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _get_cfg(self, group: str, key: str, default):
        if group in self.config and isinstance(self.config[group], dict) and key in self.config[group]:
            return self.config[group][key]
        return self.config.get(key, default)

    def _is_whitelisted(self, gid_str: str) -> bool:
        whitelist_cfg = self._get_cfg("general", "whitelist", [])
        if isinstance(whitelist_cfg, str):
            whitelist = [k.strip() for k in whitelist_cfg.split(',') if k.strip()]
        elif isinstance(whitelist_cfg, list) or isinstance(whitelist_cfg, tuple):
            whitelist = [str(x).strip() for x in whitelist_cfg if str(x).strip()]
        else:
            whitelist = []
        return gid_str in whitelist

    async def save_pending_leaves(self):
        try: await self.put_kv_data("pending_leaves", self.pending_leaves)
        except Exception as e: logger.error(f"保存 pending_leaves 时发生异常: {e}")

    async def save_mute_data(self):
        try: await self.put_kv_data("mute_stats", self.mute_stats)
        except Exception as e: logger.error(f"保存 mute_stats 时发生异常: {e}")

    async def save_hostile_stats(self):
        try: await self.put_kv_data("hostile_stats", self.hostile_stats)
        except Exception as e: logger.error(f"保存 hostile_stats 时发生异常: {e}")

    async def _clear_group_data(self, gid_str: str, clear_mute=True, clear_pending=True, clear_hostile=True):
        """ 聚合统一的并发清理入口，遵循 DRY 避免多处脏数据和重复粘贴 """
        dirty_mute, dirty_pending, dirty_hostile = False, False, False
        if clear_mute and gid_str in self.mute_stats:
            del self.mute_stats[gid_str]
            dirty_mute = True
        if clear_pending and gid_str in self.pending_leaves:
            del self.pending_leaves[gid_str]
            dirty_pending = True
        if clear_hostile and gid_str in self.hostile_stats:
            del self.hostile_stats[gid_str]
            dirty_hostile = True
            
        if dirty_mute: await self.save_mute_data()
        if dirty_pending: await self.save_pending_leaves()
        if dirty_hostile: await self.save_hostile_stats()

    async def _execute_delayed_leave(self, event, target_gid_str, wait_sec, msg):
        if wait_sec > 0:
            await asyncio.sleep(wait_sec)
        
        if target_gid_str not in self.pending_leaves:
            return
            
        try:
            await event.bot.send_group_msg(group_id=int(target_gid_str), message=msg)
        except Exception as e:
            logger.error(f"被禁言退群前发送遗言失败（可能是权限不足）: {e}")
            
        try:
            await event.bot.set_group_leave(group_id=int(target_gid_str))
            await self._clear_group_data(target_gid_str, clear_mute=True, clear_pending=True, clear_hostile=True)
        except Exception as e:
            logger.error(f"尝试自动退群失败: {e}")

    async def _handle_mute_increase(self, event, gid_str, group_id, self_id, duration, use_expected, max_mute_count, max_mute_duration):
        self.mute_stats[gid_str]["count"] += 1
        self.mute_stats[gid_str]["current_ban_start"] = time.time()
        
        if use_expected:
            expected_h = duration / 3600.0
            self.mute_stats[gid_str]["duration"] += expected_h
            self.mute_stats[gid_str]["last_expected"] = expected_h
        
        await self.save_mute_data()
        
        curr_count = self.mute_stats[gid_str]["count"]
        curr_dur = self.mute_stats[gid_str]["duration"]
        
        future_dur = curr_dur if use_expected else curr_dur + (duration / 3600.0)
        logger.info(f"bot在群 {group_id} 被禁言 {duration} 秒，当前累计次数: {curr_count}，当前已锁定时长: {curr_dur:.2f} 小时")
        
        should_leave = False
        leave_reason = ""
        
        if max_mute_count > 0 and curr_count >= max_mute_count:
            should_leave = True
            leave_reason = f"被禁言次数达到 {max_mute_count} 次"
        elif max_mute_duration > 0 and future_dur >= max_mute_duration:
            should_leave = True
            leave_reason = f"预计被禁言时长将达到 {max_mute_duration} 小时"
            
        if should_leave:
            leave_message = self._get_cfg("general", "leave_message", "看来这个群不欢迎我，退了退了")
            logger.info(f"{leave_reason}，准备录入退群计划: {group_id}")
            
            # 保留 mute_stats 直到真正发生成员清理跳出事件，以防在此期间被赦免拯救
            
            run_at = time.time() + duration + 1
            self.pending_leaves[gid_str] = {"run_at": run_at, "message": leave_message}
            await self.save_pending_leaves()
            
            self._create_task(self._execute_delayed_leave(event, gid_str, duration + 1, leave_message))
        else:
            self._update_warning_card(event, group_id, self_id, curr_count, future_dur)

    async def _handle_mute_decrease(self, event, gid_str, group_id, self_id, use_expected, max_mute_count, max_mute_duration):
        if "current_ban_start" in self.mute_stats.get(gid_str, {}):
            actual_duration_sec = time.time() - self.mute_stats[gid_str]["current_ban_start"]
            del self.mute_stats[gid_str]["current_ban_start"]
            
            revoke_count = self._get_cfg("mute_trigger", "revoke_count_on_unmute", True)
            if revoke_count:
                # 得到管理赦免，不仅停止计算时间，还同步撤销 1 次禁言次数惩罚
                self.mute_stats[gid_str]["count"] = max(0, self.mute_stats[gid_str].get("count", 1) - 1)
            
            if use_expected:
                # 如果之前采用的是预期时间直接扣除，则回撤当时加的惩罚值，并换成真实坐牢的秒数
                last_exp = self.mute_stats[gid_str].pop("last_expected", 0.0)
                self.mute_stats[gid_str]["duration"] -= last_exp
                self.mute_stats[gid_str]["duration"] += (actual_duration_sec / 3600.0)
            else:
                self.mute_stats[gid_str]["duration"] += (actual_duration_sec / 3600.0)
                
            self.mute_stats[gid_str]["duration"] = max(0.0, self.mute_stats[gid_str]["duration"])
            await self.save_mute_data()
        
        curr_count = self.mute_stats.get(gid_str, {}).get("count", 0)
        curr_dur = self.mute_stats.get(gid_str, {}).get("duration", 0.0)
        logger.info(f"群 {group_id} 禁言提前解除，赦免后当前剩余锁定次数: {curr_count}，锁定实际时长: {curr_dur:.2f} 小时")
        
        should_leave = False
        if max_mute_count > 0 and curr_count >= max_mute_count:
            should_leave = True
        elif max_mute_duration > 0 and curr_dur >= max_mute_duration:
            should_leave = True
            
        if should_leave:
            leave_message = self._get_cfg("general", "leave_message", "看来这个群不欢迎我，退了退了")
            await self._clear_group_data(gid_str, clear_mute=True, clear_pending=True, clear_hostile=True)
                
            try:
                await event.bot.send_group_msg(group_id=int(group_id), message=leave_message)
            except Exception as e:
                logger.error(f"发送强制告别遗言异常: {e}")
            try:
                await event.bot.set_group_leave(group_id=int(group_id))
            except Exception as e:
                logger.error(f"解除禁言后立刻抛错无法退群: {e}")
        else: 
            if gid_str in self.pending_leaves:
                logger.info(f"提前解除禁言，撤销原定退群计划: {group_id}")
                await self._clear_group_data(gid_str, clear_mute=False, clear_pending=True, clear_hostile=False)
            self._update_warning_card(event, group_id, self_id, curr_count, curr_dur)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=50)
    async def on_group_notice(self, event: AiocqhttpMessageEvent):
        raw_message = getattr(event.message_obj, "raw_message", None)
        if not raw_message or not isinstance(raw_message, dict):
            return

        if raw_message.get("post_type") != "notice":
            return
            
        if raw_message.get("notice_type") == "group_ban":
            user_id = raw_message.get("user_id")
            self_id = int(event.get_self_id())
            if user_id != self_id:
                return 
                
            group_id = raw_message.get("group_id")
            
            try: duration = float(raw_message.get("duration", 0))
            except (ValueError, TypeError): duration = 0.0
            
            max_mute_count = self._get_cfg("mute_trigger", "max_mute_count", 3)
            try: max_mute_duration = float(self._get_cfg("mute_trigger", "max_mute_duration", 0.0))
            except Exception: max_mute_duration = 0.0
                
            use_expected_raw = self._get_cfg("mute_trigger", "use_expected_mute_duration_for_leave", "expected")
            use_expected = use_expected_raw if isinstance(use_expected_raw, bool) else (use_expected_raw == "expected")
            
            if max_mute_count <= 0 and max_mute_duration <= 0: return
                
            gid_str = str(group_id)
            if self._is_whitelisted(gid_str): return
                
            if gid_str not in self.mute_stats:
                self.mute_stats[gid_str] = {"count": 0, "duration": 0.0}
            
            if duration > 0:
                await self._handle_mute_increase(event, gid_str, group_id, self_id, duration, use_expected, max_mute_count, max_mute_duration)
            else:
                await self._handle_mute_decrease(event, gid_str, group_id, self_id, use_expected, max_mute_count, max_mute_duration)
                    
    def _update_warning_card(self, event, group_id, self_id, curr_count, projected_dur):
        async def do_update():
            warning_count = self._get_cfg("mute_trigger", "warning_mute_count_left", 1)
            warning_dur = self._get_cfg("mute_trigger", "warning_mute_duration_left", 10)
            max_mute_count = self._get_cfg("mute_trigger", "max_mute_count", 3)
            try: max_mute_duration = float(self._get_cfg("mute_trigger", "max_mute_duration", 0.0))
            except Exception: max_mute_duration = 0.0
            
            rem_count = max_mute_count - curr_count if max_mute_count > 0 else -1
            rem_dur_mins = (max_mute_duration - projected_dur) * 60.0 if max_mute_duration > 0 else -1.0
            
            trigger_warn = False
            if max_mute_count > 0 and 0 < rem_count <= warning_count: trigger_warn = True
            if max_mute_duration > 0 and 0 < rem_dur_mins <= warning_dur: trigger_warn = True
                
            if trigger_warn:
                parts = []
                if max_mute_count > 0 and rem_count > 0: parts.append(f"{rem_count}次")
                if max_mute_duration > 0 and rem_dur_mins > 0: parts.append(f"{rem_dur_mins:.0f}分钟")
                if parts:
                    try:
                        info = await event.bot.get_group_member_info(group_id=int(group_id), user_id=int(self_id))
                        orig_card = info.get("card", "")
                        if not orig_card: orig_card = info.get("nickname", "Bot")
                        # 非贪婪匹配，避免切断正确的昵称组合
                        clean_card = re.sub(r'\(再禁言.*?可退群\)', '', orig_card).strip()
                        new_card = f"{clean_card}(再禁言{'或'.join(parts)}即可退群)"
                        await event.bot.set_group_card(group_id=int(group_id), user_id=int(self_id), card=new_card)
                    except Exception as e:
                        logger.error(f"被禁言后未能成功修改群名片以做出警告, 可能是权限等问题: {e}")
        
        self._create_task(do_update())

    async def initialize(self):
        try:
            self.mute_stats = await self.get_kv_data("mute_stats", {})
            for k, v in list(self.mute_stats.items()):
                if isinstance(v, int):
                    self.mute_stats[k] = {"count": v, "duration": 0}
            
            self.pending_leaves = await self.get_kv_data("pending_leaves", {})
            self.hostile_stats = await self.get_kv_data("hostile_stats", {})
        except Exception as e:
            logger.error(f"插件核心 KV 数据装载阶段触发致命错误: {e}")
            self.mute_stats, self.pending_leaves, self.hostile_stats = {}, {}, {}

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def check_pending_leaves_on_msg(self, event: AstrMessageEvent):
        if not self.pending_leaves: return
            
        now = time.time()
        gids_to_del = []
        for gid_str, data in list(self.pending_leaves.items()):
            if self._is_whitelisted(gid_str):
                gids_to_del.append(gid_str)
                continue
                
            if now >= data["run_at"]:
                logger.info(f"检测到可能因重启中断的退群任务，尝试补发并退出: {gid_str}")
                msg = data.get("message", "")
                try:
                    if msg: await event.bot.send_group_msg(group_id=int(gid_str), message=msg)
                except Exception as e: logger.error(f"发送积存的告别消息失败: {e}")
                
                try: await event.bot.set_group_leave(group_id=int(gid_str))
                except Exception as e: logger.error(f"尝试补发积存的自动退群失败: {e}")
                gids_to_del.append(gid_str)
                
        for g in gids_to_del:
            await self._clear_group_data(g, clear_mute=False, clear_pending=True, clear_hostile=False)

    @filter.on_llm_response()
    async def process_hostility(self, event: AstrMessageEvent, resp: LLMResponse):
        keywords_cfg = self._get_cfg("llm_trigger", "hostile_keywords", [])
        if isinstance(keywords_cfg, str):
            keywords = [k.strip() for k in keywords_cfg.split(",") if k.strip()]
        elif isinstance(keywords_cfg, list) or isinstance(keywords_cfg, tuple):
            keywords = [str(k).strip() for k in keywords_cfg if str(k).strip()]
        else: keywords = []

        if not keywords: return
            
        message_str = getattr(event, "message_str", "")
        if not message_str and hasattr(event, "message_obj"):
            message_str = getattr(event.message_obj, "message_str", "")
        if not message_str: return
            
        if not any(k in message_str for k in keywords): return
            
        group_id = event.get_group_id()
        if not group_id: return
            
        gid_str = str(group_id)
        if self._is_whitelisted(gid_str): return
        
        provider_id = self._get_cfg("llm_trigger", "hostile_llm_provider", "").strip()
        if not provider_id:
            try: provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            except Exception as e:
                logger.error(f"获取当前默认模型提供方出错: {e}")
                provider_id = None
        
        default_prompt = (
            "你是一个专门用于意图判定的安全中枢助手。\n"
            "请判定以下目标对话是否明确表现出对bot的厌恶、反感、敌对或驱逐意图。\n"
            "注意：严格只回答“是”或“否”，绝对不要输出任何额外的内容标记。"
        )
        hostile_prompt_cfg = self._get_cfg("llm_trigger", "hostile_prompt", {})
        if isinstance(hostile_prompt_cfg, dict):
            base_prompt = hostile_prompt_cfg.get("prompt_template", default_prompt)
        else:
            base_prompt = hostile_prompt_cfg if hostile_prompt_cfg else default_prompt
        base_prompt = str(base_prompt).strip()
        
        prompt = f'''{base_prompt}
---
目标会话：
<msg>{message_str}</msg>'''
        
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id if provider_id else None,
                prompt=prompt
            )
            
            if not llm_resp or not hasattr(llm_resp, "completion_text") or not llm_resp.completion_text: return
                
            result = llm_resp.completion_text.strip()
            
            if "是" in result:
                self.hostile_stats[gid_str] = self.hostile_stats.get(gid_str, 0) + 1
                await self.save_hostile_stats()
                
                curr_hostile = self.hostile_stats[gid_str]
                max_hostile = self._get_cfg("llm_trigger", "max_hostile_count", 3)
                
                logger.info(f"群 {group_id} 敌意发言计数 +1，当前累计: {curr_hostile}/{max_hostile}")
                
                left = max_hostile - curr_hostile
                warning_threshold = self._get_cfg("llm_trigger", "warning_hostile_count_left", 2)
                
                if curr_hostile >= max_hostile:
                    leave_message = self._get_cfg("general", "leave_message", "看来这个群不欢迎我，退了退了")
                    
                    await self._clear_group_data(gid_str, clear_mute=True, clear_pending=True, clear_hostile=True)
                    
                    try: await event.bot.send_group_msg(group_id=int(group_id), message=leave_message)
                    except Exception as e: logger.error(f"敌意检测后发送告别词出错: {e}")
                        
                    try: await event.bot.set_group_leave(group_id=int(group_id))
                    except Exception as e: logger.error(f"因敌意言论触发自动退群失败（无法清退）: {e}")
                elif warning_threshold > 0 and 0 < left <= warning_threshold:
                    # 进入敌意倒计时警戒线，向群内发送警告消息
                    warn_tpl = self._get_cfg("llm_trigger", "warning_hostile_message",
                        "请注意你们的言辞，当前已累计{count}次敌意发言，再来{left}次我就退群了。")
                    try:
                        warn_msg = warn_tpl.format(count=curr_hostile, max=max_hostile, left=left)
                    except (KeyError, ValueError):
                        warn_msg = f"请注意你们的言辞，当前已累计{curr_hostile}次敌意发言，再来{left}次我就退群了。"
                    try:
                        await event.bot.send_group_msg(group_id=int(group_id), message=warn_msg)
                    except Exception as e:
                        logger.error(f"发送敌意警告消息失败: {e}")
        except Exception as e:
            logger.error(f"调用模型深度判定敌意指控报错: {e}")

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def process_custom_command(self, event: AstrMessageEvent):
        custom_cmd = self._get_cfg("command_trigger", "custom_command", "").strip()
        if not custom_cmd or custom_cmd == "/bye": return
            
        message_str = getattr(event, "message_str", "")
        if not message_str and hasattr(event, "message_obj"):
            message_str = getattr(event.message_obj, "message_str", "")
            
        if message_str.strip() == custom_cmd:
            await self._execute_manual_leave(event, yield_result=False)

    async def _execute_manual_leave(self, event: AstrMessageEvent, yield_result=False):
        try:
            enabled = self._get_cfg("command_trigger", "enabled", True)
            group_id = event.get_group_id()
            
            if not group_id:
                msg = "该指令只能在群聊中使用。"
                if yield_result: return event.plain_result(msg)
                else:
                    try: await event.bot.send_msg(event.get_sender_id(), msg)
                    except: pass
                return

            if not enabled:
                msg = "已禁用主动退群功能。"
                if yield_result: return event.plain_result(msg)
                else: 
                    try: await event.bot.send_group_msg(group_id=int(group_id), message=msg)
                    except: pass
                return

            gid_str = str(group_id)
            if self._is_whitelisted(gid_str):
                msg = "当前群聊处于被保护的白名单中，插件受禁，无法执行退群。"
                if yield_result: return event.plain_result(msg)
                else: 
                    try: await event.bot.send_group_msg(group_id=int(group_id), message=msg)
                    except: pass
                return

            leave_message = self._get_cfg("general", "leave_message", "看来这个群不欢迎我，退了退了")
            
            try: await event.bot.send_group_msg(group_id=int(group_id), message=leave_message)
            except Exception as e: logger.error(f"发送主动退群告别语异常: {e}")
                
            await event.bot.set_group_leave(group_id=int(group_id))
            
            await self._clear_group_data(gid_str, clear_mute=True, clear_pending=True, clear_hostile=True)
                
            if yield_result:
                return event.plain_result("指令已执行强制退群清理模块。")
        except Exception as e:
            msg = f"操作严重受阻，遇到报错: {e}。可能底端 API 平台不支持离开指令。"
            if yield_result: return event.plain_result(msg)
            else: 
                try: await event.bot.send_group_msg(group_id=int(group_id), message=msg)
                except: pass

    @filter.command("bye")
    async def bye(self, event: AstrMessageEvent):
        try:
            res = await self._execute_manual_leave(event, yield_result=True)
            if res: yield res
        except Exception as e: logger.error(f"处理 /bye 指令时报错: {e}")

    @filter.command("bye_stats")
    @filter.permission_type(PermissionType.ADMIN)
    async def bye_stats(self, event: AstrMessageEvent):
        try:
            if not self.mute_stats and not self.hostile_stats:
                yield event.plain_result("🎉 报告：当前没有任何群聊有对 bot 的不友善对待记录。环境很清爽！")
                return

            report_lines = ["📊 【各群大仇恨之书】"]
            all_groups = set(list(self.mute_stats.keys()) + list(self.hostile_stats.keys()))
            
            if not all_groups:
                yield event.plain_result("暂无统计信息。")
                return

            for gid in all_groups:
                lines = [f"▸ 群聊ID: {gid}"]
                m_info = self.mute_stats.get(gid)
                h_info = self.hostile_stats.get(gid)
                
                is_pending = "🔒 (近期在禁言席位中待决)" if (m_info and "current_ban_start" in m_info) else ""
                
                if m_info:
                    count = m_info.get("count", 0)
                    dur = m_info.get("duration", 0.0)
                    if count > 0 or dur > 0:
                        lines.append(f"   🤬 被禁言次数: {count}次 | 累计涉狱时长: {dur:.2f}小时 {is_pending}")
                
                if h_info and h_info > 0:
                    lines.append(f"   ⚠️ 被LLM判定遭到敌视次数: {h_info}次")
                    
                if len(lines) > 1: report_lines.append("\n".join(lines))
                    
            if len(report_lines) == 1: yield event.plain_result("🎉 数据已清洗，当前暂时没有产生违规指标。")
            else: yield event.plain_result("\n".join(report_lines))
                
        except Exception as e:
            yield event.plain_result(f"获取统计报告时遇到意外错误: {e}")

    @filter.command("bye_clear")
    @filter.permission_type(PermissionType.ADMIN)
    async def bye_clear(self, event: AstrMessageEvent, target_group: str = ""):
        try:
            target_group = target_group.strip()
            
            if target_group:
                cleared = False
                if target_group in self.mute_stats:
                    cleared = True
                if target_group in self.hostile_stats:
                    cleared = True
                    
                if cleared:
                    await self._clear_group_data(target_group, clear_mute=True, clear_pending=False, clear_hostile=True)
                    yield event.plain_result(f"🧹 已成功清理群聊 {target_group} 的所有不友好记录！")
                else:
                    yield event.plain_result(f"⚠️ 未找到群聊 {target_group} 的相关记录，它本身就很友好。")
            else:
                self.mute_stats.clear()
                self.hostile_stats.clear()
                await self.save_mute_data()
                await self.save_hostile_stats()
                yield event.plain_result("🧹 已成功清空所有群聊的不友好记录！《大仇恨之书》已彻底重置。")
                
        except Exception as e:
            yield event.plain_result(f"清理记录时遇到意外错误: {e}")

    async def terminate(self):
        pass
