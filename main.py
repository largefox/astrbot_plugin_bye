import time
import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType

@register("astrbot_plugin_bye", "largefox", "群聊氛围不合适或不欢迎机器人的时候，让机器人主动退群，保护机器人身心健康，节省Tokens。", "1.0.0")
class ByePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.mute_stats = {}
        self.pending_leaves = {}
        self.hostile_stats = {}

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
        try:
            await self.put_kv_data("pending_leaves", self.pending_leaves)
        except Exception as e:
            logger.error(f"保存 pending_leaves 时发生异常: {e}")

    async def save_mute_data(self):
        try:
            await self.put_kv_data("mute_stats", self.mute_stats)
        except Exception as e:
            logger.error(f"保存 mute_stats 时发生异常: {e}")

    async def save_hostile_stats(self):
        try:
            await self.put_kv_data("hostile_stats", self.hostile_stats)
        except Exception as e:
            logger.error(f"保存 hostile_stats 时发生异常: {e}")

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_notice(self, event: AiocqhttpMessageEvent):
        """监听被禁言等提醒事件"""
        raw_message = getattr(event.message_obj, "raw_message", None)
        if not raw_message or not isinstance(raw_message, dict):
            return

        if raw_message.get("post_type") != "notice":
            return
            
        if raw_message.get("notice_type") == "group_ban":
            user_id = raw_message.get("user_id")
            self_id = int(event.get_self_id())
            if user_id != self_id:
                return # Not banning the bot
                
            group_id = raw_message.get("group_id")
            try:
                duration = float(raw_message.get("duration", 0))
            except (ValueError, TypeError):
                # 防止平台下发的非常规格式导致崩溃
                logger.error(f"解析禁言指令时长参数错误: {raw_message.get('duration')}")
                duration = 0.0
            
            max_mute_count = self._get_cfg("mute_trigger", "max_mute_count", 3)
            try:
                max_mute_duration = float(self._get_cfg("mute_trigger", "max_mute_duration", 0.0))
            except Exception:
                max_mute_duration = 0.0
                
            use_expected_raw = self._get_cfg("mute_trigger", "use_expected_mute_duration_for_leave", "expected")
            use_expected = use_expected_raw if isinstance(use_expected_raw, bool) else (use_expected_raw == "expected")
            
            if max_mute_count <= 0 and max_mute_duration <= 0:
                return
                
            gid_str = str(group_id)
            if self._is_whitelisted(gid_str):
                return
                
            if gid_str not in self.mute_stats:
                self.mute_stats[gid_str] = {"count": 0, "duration": 0.0}
            
            import time
            import asyncio
            import re
            
            if duration > 0:
                # 触发禁言
                self.mute_stats[gid_str]["count"] += 1
                self.mute_stats[gid_str]["current_ban_start"] = time.time()
                
                if use_expected:
                    self.mute_stats[gid_str]["duration"] += (duration / 3600.0)
                
                await self.save_mute_data()
                
                curr_count = self.mute_stats[gid_str]["count"]
                curr_dur = self.mute_stats[gid_str]["duration"]
                
                future_dur = curr_dur if use_expected else curr_dur + (duration / 3600.0)
                logger.info(f"机器人在群 {group_id} 被禁言 {duration} 秒，当前累计次数: {curr_count}，当前已锁定时长: {curr_dur:.2f} 小时")
                
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
                    
                    if gid_str in self.mute_stats:
                        del self.mute_stats[gid_str]
                        await self.save_mute_data()
                    
                    run_at = time.time() + duration + 1
                    self.pending_leaves[gid_str] = {"run_at": run_at, "message": leave_message}
                    await self.save_pending_leaves()
                    
                    async def execute_leave(target_gid_str, wait_sec, msg):
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
                            if target_gid_str in self.pending_leaves:
                                del self.pending_leaves[target_gid_str]
                                await self.save_pending_leaves()
                            if target_gid_str in self.hostile_stats:
                                del self.hostile_stats[target_gid_str]
                                await self.save_hostile_stats()
                        except Exception as e:
                            logger.error(f"尝试自动退群失败: {e}")
                    
                    asyncio.create_task(execute_leave(gid_str, duration + 1, leave_message))
                else:
                    self._update_warning_card(event, group_id, self_id, curr_count, future_dur)
                    
            else:
                # duration == 0, 解除禁言
                if "current_ban_start" in self.mute_stats.get(gid_str, {}):
                    actual_duration_sec = time.time() - self.mute_stats[gid_str]["current_ban_start"]
                    del self.mute_stats[gid_str]["current_ban_start"]
                    if not use_expected:
                        self.mute_stats[gid_str]["duration"] += (actual_duration_sec / 3600.0)
                    await self.save_mute_data()
                
                curr_count = self.mute_stats[gid_str]["count"]
                curr_dur = self.mute_stats[gid_str]["duration"]
                logger.info(f"群 {group_id} 禁言提前解除，当前累计次数: {curr_count}，已锁定实际时长: {curr_dur:.2f} 小时")
                
                should_leave = False
                if max_mute_count > 0 and curr_count >= max_mute_count:
                    should_leave = True
                elif max_mute_duration > 0 and curr_dur >= max_mute_duration:
                    should_leave = True
                    
                if should_leave:
                    leave_message = self._get_cfg("general", "leave_message", "看来这个群不欢迎我，退了退了")
                    if gid_str in self.pending_leaves:
                        del self.pending_leaves[gid_str]
                        await self.save_pending_leaves()
                    if gid_str in self.mute_stats:
                        del self.mute_stats[gid_str]
                        await self.save_mute_data()
                    if gid_str in self.hostile_stats:
                        del self.hostile_stats[gid_str]
                        await self.save_hostile_stats()
                        
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
                        logger.info(f"提前解除禁言成功拯救此时的机器人，撤销原定退群计划: {group_id}")
                        del self.pending_leaves[gid_str]
                        await self.save_pending_leaves()
                    self._update_warning_card(event, group_id, self_id, curr_count, curr_dur)
                    
    def _update_warning_card(self, event, group_id, self_id, curr_count, projected_dur):
        import asyncio
        async def do_update():
            warning_count = self._get_cfg("mute_trigger", "warning_mute_count_left", 1)
            warning_dur = self._get_cfg("mute_trigger", "warning_mute_duration_left", 10)
            max_mute_count = self._get_cfg("mute_trigger", "max_mute_count", 3)
            try:
                max_mute_duration = float(self._get_cfg("mute_trigger", "max_mute_duration", 0.0))
            except Exception:
                max_mute_duration = 0.0
            
            rem_count = max_mute_count - curr_count if max_mute_count > 0 else -1
            rem_dur_mins = (max_mute_duration - projected_dur) * 60.0 if max_mute_duration > 0 else -1.0
            
            trigger_warn = False
            if max_mute_count > 0 and 0 < rem_count <= warning_count:
                trigger_warn = True
            if max_mute_duration > 0 and 0 < rem_dur_mins <= warning_dur:
                trigger_warn = True
                
            if trigger_warn:
                import re
                parts = []
                if max_mute_count > 0 and rem_count > 0:
                    parts.append(f"{rem_count}次")
                if max_mute_duration > 0 and rem_dur_mins > 0:
                    parts.append(f"{rem_dur_mins:.0f}分钟")
                if parts:
                    try:
                        info = await event.bot.get_group_member_info(group_id=int(group_id), user_id=int(self_id))
                        orig_card = info.get("card", "")
                        if not orig_card:
                            orig_card = info.get("nickname", "Bot")
                        clean_card = re.sub(r'\(再禁言.*可退群\)', '', orig_card).strip()
                        new_card = f"{clean_card}(再禁言{'或'.join(parts)}即可退群)"
                        await event.bot.set_group_card(group_id=int(group_id), user_id=int(self_id), card=new_card)
                    except Exception as e:
                        logger.error(f"被禁言后未能成功修改群名片以做出警告, 可能是权限等问题: {e}")
        asyncio.create_task(do_update())

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        try:
            self.mute_stats = await self.get_kv_data("mute_stats", {})
            # 兼容之前只有数字格式的老数据
            for k, v in list(self.mute_stats.items()):
                if isinstance(v, int):
                    self.mute_stats[k] = {"count": v, "duration": 0}
            
            self.pending_leaves = await self.get_kv_data("pending_leaves", {})
            self.hostile_stats = await self.get_kv_data("hostile_stats", {})
        except Exception as e:
            logger.error(f"插件核心 KV 数据装载阶段触发致命错误: {e}")
            self.mute_stats = {}
            self.pending_leaves = {}
            self.hostile_stats = {}

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def check_pending_leaves_on_msg(self, event: AstrMessageEvent):
        """兜底逻辑：在收到任何群消息时，检查是否存在因程序重启而中断的退群任务"""
        if not self.pending_leaves:
            return
            
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
                    if msg:
                        await event.bot.send_group_msg(group_id=int(gid_str), message=msg)
                except Exception as e:
                    logger.error(f"发送积存的告别消息失败: {e}")
                
                try:
                    await event.bot.set_group_leave(group_id=int(gid_str))
                except Exception as e:
                    logger.error(f"尝试补发积存的自动退群失败: {e}")
                gids_to_del.append(gid_str)
                
        for g in gids_to_del:
            if g in self.pending_leaves:
                del self.pending_leaves[g]
        if gids_to_del:
            await self.save_pending_leaves()

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def process_hostility(self, event: AstrMessageEvent):
        """监听普通群聊消息进行大语言模型敌意判定"""
        keywords_cfg = self._get_cfg("llm_trigger", "hostile_keywords", [])
        if isinstance(keywords_cfg, str):
            keywords = [k.strip() for k in keywords_cfg.split(",") if k.strip()]
        elif isinstance(keywords_cfg, list) or isinstance(keywords_cfg, tuple):
            keywords = [str(k).strip() for k in keywords_cfg if str(k).strip()]
        else:
            keywords = []

        if not keywords:
            return
            
        message_str = getattr(event, "message_str", "")
        if not message_str and hasattr(event, "message_obj"):
            message_str = getattr(event.message_obj, "message_str", "")
        if not message_str:
            return
            
        if not any(k in message_str for k in keywords):
            return
            
        # 触发了关键词，交给大模型做语义二分类
        group_id = event.get_group_id()
        gid_str = str(group_id)
        
        if self._is_whitelisted(gid_str):
            return
        
        provider_id = self._get_cfg("llm_trigger", "hostile_llm_provider", "").strip()
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            except Exception as e:
                logger.error(f"获取当前默认模型提供方出错: {e}")
                provider_id = None
        
        prompt = f'''你是一个专门用于意图判定的安全中枢助手。
请判定以下群聊消息是否明确表现出对机器人（Bot）的厌恶、反感、敌对或驱逐意图。
注意：只需回答“是”或“否”，不要输出任何其他内容。
消息内容：{message_str}'''
        
        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id if provider_id else None,
                prompt=prompt
            )
            
            if not llm_resp or not hasattr(llm_resp, "completion_text") or not llm_resp.completion_text:
                logger.error("大模型返回检测结果为空或格式不合法（无法解析 completion_text）。")
                return
                
            result = llm_resp.completion_text.strip()
            logger.info(f"对疑似敌意消息 '{message_str}' 的模型判定结果: {result}")
            
            if "是" in result:
                self.hostile_stats[gid_str] = self.hostile_stats.get(gid_str, 0) + 1
                await self.save_hostile_stats()
                
                curr_hostile = self.hostile_stats[gid_str]
                max_hostile = self._get_cfg("llm_trigger", "max_hostile_count", 3)
                
                logger.info(f"群 {group_id} 敌意发言计数 +1，当前累计: {curr_hostile}/{max_hostile}")
                
                if curr_hostile >= max_hostile:
                    leave_message = self._get_cfg("general", "leave_message", "看来这个群不欢迎我，退了退了")
                    logger.info(f"敌意发言次数已达上限 {max_hostile} 次，准备执行退群: {group_id}")
                    
                    if gid_str in self.hostile_stats:
                        del self.hostile_stats[gid_str]
                        await self.save_hostile_stats()
                    
                    if gid_str in self.mute_stats:
                        del self.mute_stats[gid_str]
                        await self.save_mute_data()
                        
                    if gid_str in self.pending_leaves:
                        del self.pending_leaves[gid_str]
                        await self.save_pending_leaves()
                    
                    try:
                        await event.bot.send_group_msg(group_id=int(group_id), message=leave_message)
                    except Exception as e:
                        logger.error(f"敌意检测后发送告别词出错: {e}")
                        
                    try:
                        await event.bot.set_group_leave(group_id=int(group_id))
                    except Exception as e:
                        logger.error(f"因敌意言论触发自动退群失败（无法清退）: {e}")
        except Exception as e:
            logger.error(f"调用模型深度判定敌意指控报错: {e}")

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def process_custom_command(self, event: AstrMessageEvent):
        custom_cmd = self._get_cfg("command_trigger", "custom_command", "").strip()
        if not custom_cmd or custom_cmd == "/bye":
            return
            
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

            logger.info(f"收到退群主指令请求，准备退出群聊: {group_id}")
            leave_message = self._get_cfg("general", "leave_message", "看来这个群不欢迎我，退了退了")
            
            try:
                await event.bot.send_group_msg(group_id=int(group_id), message=leave_message)
            except Exception as e:
                logger.error(f"发送主动退群告别语异常: {e}")
                
            await event.bot.set_group_leave(group_id=int(group_id))
            
            if gid_str in self.mute_stats:
                del self.mute_stats[gid_str]
                await self.save_mute_data()
            if gid_str in self.pending_leaves:
                del self.pending_leaves[gid_str]
                await self.save_pending_leaves()
            if gid_str in self.hostile_stats:
                del self.hostile_stats[gid_str]
                await self.save_hostile_stats()
                
            if yield_result:
                return event.plain_result("指令已执行强制退群清理模块。")
        except Exception as e:
            logger.error(f"退群强制执行指令致命故障: {e}")
            msg = f"操作严重受阻，遇到报错: {e}。可能底端 API 平台不支持离开指令。"
            if yield_result: return event.plain_result(msg)
            else: 
                try: await event.bot.send_group_msg(group_id=int(group_id), message=msg)
                except: pass

    # 注册指令的装饰器。指令名为 bye。注册成功后，发送 `/bye` 就会触发这个指令。
    @filter.command("bye")
    async def bye(self, event: AstrMessageEvent):
        """让机器人主动退群的指令（原生支持别名替换功能）"""
        try:
            res = await self._execute_manual_leave(event, yield_result=True)
            if res:
                yield res
        except Exception as e:
            logger.error(f"处理 /bye 指令时报错: {e}")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
