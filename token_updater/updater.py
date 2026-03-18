"""Token sync service."""
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .browser import browser_manager
from .config import config
from .database import profile_db
from .events import dashboard_events
from .logger import logger


class TokenSyncer:
    """Token 同步器。"""

    def _is_expired_access_token_error(self, error: str) -> bool:
        text = (error or "").lower()
        return "expired access token" in text or "converted to an expired access token" in text


    def __init__(self):
        self._total_sync_count = 0
        self._total_error_count = 0
        self._last_batch_time: Optional[datetime] = None

    def _resolve_target(self, profile: Dict[str, Any]) -> Tuple[str, str]:
        """优先使用 Profile 级配置，没有则回退到全局默认值。"""
        flow2api_url = (profile.get("flow2api_url") or config.flow2api_url or "").strip().rstrip("/")
        connection_token = (
            profile.get("connection_token_override") or config.connection_token or ""
        ).strip()
        return flow2api_url, connection_token

    async def _record_sync_result(
        self,
        profile: Dict[str, Any],
        target_url: str,
        success: bool,
        action: str = "",
        message: str = "",
        email: Optional[str] = None,
    ) -> None:
        status = "success" if success else "error"
        await profile_db.record_sync_event(
            profile_id=profile["id"],
            profile_name=profile["name"],
            email=email or profile.get("email"),
            target_url=target_url,
            status=status,
            action=action,
            message=message,
        )
        await dashboard_events.publish(
            "sync_result",
            {
                "profile_id": profile["id"],
                "profile_name": profile["name"],
                "status": status,
                "target_url": target_url,
                "action": action,
                "message": message,
                "email": email or profile.get("email"),
            },
        )

    async def _check_tokens_status(
        self,
        flow2api_url: str,
        connection_token: str,
        emails: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """从指定 Flow2API 查询 Token 状态。"""
        if not connection_token:
            return {"success": False, "error": "未配置 CONNECTION_TOKEN"}
        if not flow2api_url:
            return {"success": False, "error": "未配置 Flow2API 地址"}

        url = f"{flow2api_url}/api/plugin/check-tokens"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                payload = {}
                if emails:
                    payload["emails"] = emails

                response = await client.post(
                    url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {connection_token}",
                    },
                )

                if response.status_code in (404, 405):
                    return {
                        "success": True,
                        "tokens": [],
                        "needs_refresh_emails": [],
                        "mode": "unsupported",
                        "message": "check-tokens endpoint unavailable on target Flow2API",
                    }
                if response.status_code != 200:
                    return {"success": False, "error": f"HTTP {response.status_code}"}

                data = response.json()
                tokens = data.get("tokens", [])
                needs_refresh_emails = [
                    token["email"]
                    for token in tokens
                    if token.get("needs_refresh") and token.get("is_active")
                ]
                return {
                    "success": True,
                    "tokens": tokens,
                    "needs_refresh_emails": needs_refresh_emails,
                }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    async def sync_profile(self, profile_id: int) -> Dict[str, Any]:
        """同步单个 Profile。"""
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        flow2api_url, connection_token = self._resolve_target(profile)
        if not flow2api_url or not connection_token:
            error = "未配置完整的 Flow2API 地址或连接 Token"
            await profile_db.update_profile(
                profile_id,
                last_sync_time=datetime.now().isoformat(),
                last_sync_result=f"failed: {error}",
                error_count=profile.get("error_count", 0) + 1,
            )
            self._total_error_count += 1
            await self._record_sync_result(profile, flow2api_url, False, message=error)
            return {"success": False, "error": error, "target_url": flow2api_url}

        logger.info(f"[{profile['name']}] 开始同步 -> {flow2api_url}")

        token = await browser_manager.extract_token(profile_id)
        if not token:
            error = "无法提取 Token，请先登录"
            await profile_db.update_profile(
                profile_id,
                last_sync_time=datetime.now().isoformat(),
                last_sync_result="failed: no token",
                error_count=profile.get("error_count", 0) + 1,
            )
            self._total_error_count += 1
            await self._record_sync_result(profile, flow2api_url, False, message=error)
            return {"success": False, "error": error, "target_url": flow2api_url}

        logger.info(f"[{profile['name']}] 提取到 Token: {token[:20]}...{token[-10:]}")
        result = await self._push_to_flow2api(token, flow2api_url, connection_token)

        if (not result.get("success")) and self._is_expired_access_token_error(result.get("error", "")):
            logger.warning(f"[{profile['name']}] Flow2API 返回 expired access token，自动执行一次登录浏览器启停恢复后重试")
            activation = await browser_manager.recover_session_via_login_cycle(profile_id)
            if activation.get("success"):
                retry_token = activation.get("token") or await browser_manager.extract_token(profile_id)
                if retry_token:
                    result = await self._push_to_flow2api(retry_token, flow2api_url, connection_token)
                    if result.get("success"):
                        msg = result.get("message", "")
                        result["message"] = f"[auto-login-cycle-reactivated] {msg}".strip()
                else:
                    result = {"success": False, "error": "自动登录浏览器启停恢复后仍无法提取 token"}
            else:
                result = {"success": False, "error": f"自动登录浏览器启停恢复失败: {activation.get('error', 'unknown')}"}

        if result["success"]:
            await profile_db.update_profile(
                profile_id,
                email=result.get("email", profile.get("email")),
                last_sync_time=datetime.now().isoformat(),
                last_sync_result=f"success: {result.get('action', 'synced')}",
                sync_count=profile.get("sync_count", 0) + 1,
            )
            self._total_sync_count += 1
            logger.info(f"[{profile['name']}] 同步成功")
            await self._record_sync_result(
                profile,
                flow2api_url,
                True,
                action=result.get("action", "synced"),
                message=result.get("message", ""),
                email=result.get("email"),
            )
        else:
            await profile_db.update_profile(
                profile_id,
                last_sync_time=datetime.now().isoformat(),
                last_sync_result=f"failed: {result.get('error', 'unknown')}",
                error_count=profile.get("error_count", 0) + 1,
            )
            self._total_error_count += 1
            logger.error(f"[{profile['name']}] 同步失败: {result.get('error')}")
            await self._record_sync_result(
                profile,
                flow2api_url,
                False,
                message=result.get("error", "unknown"),
            )

        return {**result, "target_url": flow2api_url}

    async def sync_all_profiles(self) -> Dict[str, Any]:
        """同步所有活跃 Profile（智能模式：按目标地址分组刷新）。"""
        logger.info("=" * 40)
        logger.info("开始智能同步...")

        self._last_batch_time = datetime.now()
        profiles = await profile_db.get_active_profiles()

        if not profiles:
            result = {"success": True, "total": 0, "synced": 0, "skipped": 0, "results": []}
            await dashboard_events.publish("sync_batch", result)
            return result

        grouped_profiles: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        invalid_profiles: List[Dict[str, Any]] = []

        for profile in profiles:
            flow2api_url, connection_token = self._resolve_target(profile)
            if not flow2api_url or not connection_token:
                invalid_profiles.append(profile)
                continue
            grouped_profiles[(flow2api_url, connection_token)].append(profile)

        results: List[Dict[str, Any]] = []
        success_count = 0
        error_count = 0
        skipped_count = 0

        for profile in invalid_profiles:
            flow2api_url, _ = self._resolve_target(profile)
            error = "未配置完整的 Flow2API 地址或连接 Token"
            await profile_db.update_profile(
                profile["id"],
                last_sync_time=datetime.now().isoformat(),
                last_sync_result=f"failed: {error}",
                error_count=profile.get("error_count", 0) + 1,
            )
            self._total_error_count += 1
            await self._record_sync_result(profile, flow2api_url, False, message=error)
            results.append(
                {
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    "success": False,
                    "error": error,
                    "target_url": flow2api_url,
                }
            )
            error_count += 1

        for (flow2api_url, connection_token), target_profiles in grouped_profiles.items():
            profile_emails = [profile["email"] for profile in target_profiles if profile.get("email")]
            check_result = await self._check_tokens_status(
                flow2api_url,
                connection_token,
                profile_emails or None,
            )

            if not check_result["success"]:
                logger.warning(
                    f"[{flow2api_url}] 无法查询 token 状态: {check_result.get('error')}，回退到该目标全量同步"
                )
                group_result = await self._sync_profiles_force(target_profiles)
                results.extend(group_result["results"])
                success_count += group_result["success_count"]
                error_count += group_result["error_count"]
                continue

            needs_refresh_emails = set(check_result.get("needs_refresh_emails", []))
            for profile in target_profiles:
                email = profile.get("email")
                should_sync = not email or email in needs_refresh_emails
                if should_sync:
                    result = await self.sync_profile(profile["id"])
                    results.append(
                        {
                            "profile_id": profile["id"],
                            "profile_name": profile["name"],
                            **result,
                        }
                    )
                    if result["success"]:
                        success_count += 1
                    else:
                        error_count += 1
                else:
                    skipped_count += 1
                    logger.info(f"[{profile['name']}] token 未过期，跳过")

        logger.info(
            f"智能同步完成: 成功 {success_count}, 失败 {error_count}, 跳过 {skipped_count}"
        )

        result = {
            "success": True,
            "total": len(profiles),
            "synced": success_count + error_count,
            "success_count": success_count,
            "error_count": error_count,
            "skipped": skipped_count,
            "results": results,
        }
        await dashboard_events.publish("sync_batch", result)
        return result

    async def _sync_profiles_force(self, profiles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """强制同步指定 Profile 列表。"""
        results = []
        success_count = 0
        error_count = 0

        for profile in profiles:
            result = await self.sync_profile(profile["id"])
            results.append(
                {
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    **result,
                }
            )
            if result["success"]:
                success_count += 1
            else:
                error_count += 1

        return {
            "results": results,
            "success_count": success_count,
            "error_count": error_count,
        }

    async def _sync_all_profiles_force(self) -> Dict[str, Any]:
        """强制同步所有 Profile（不检查过期状态）。"""
        profiles = await profile_db.get_active_profiles()
        group_result = await self._sync_profiles_force(profiles)

        logger.info(
            f"强制同步完成: 成功 {group_result['success_count']}, 失败 {group_result['error_count']}"
        )

        return {
            "success": True,
            "total": len(profiles),
            "success_count": group_result["success_count"],
            "error_count": group_result["error_count"],
            "results": group_result["results"],
        }

    async def _push_to_flow2api(
        self,
        session_token: str,
        flow2api_url: str,
        connection_token: str,
    ) -> Dict[str, Any]:
        """推送到指定 Flow2API。"""
        if not connection_token:
            return {"success": False, "error": "未配置 CONNECTION_TOKEN"}
        if not flow2api_url:
            return {"success": False, "error": "未配置 Flow2API 地址"}

        url = f"{flow2api_url}/api/plugin/update-token"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    url,
                    json={"session_token": session_token},
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {connection_token}",
                    },
                )

                response_text = (response.text or "").strip()
                response_json = None
                if response_text:
                    try:
                        response_json = response.json()
                    except Exception:
                        response_json = None

                if response.status_code != 200:
                    detail = None
                    if isinstance(response_json, dict):
                        detail = response_json.get("detail") or response_json.get("message")
                    error = f"HTTP {response.status_code}"
                    if detail:
                        error = f"{error}: {detail}"
                    elif response_text:
                        snippet = response_text[:300]
                        error = f"{error}: {snippet}"
                    return {"success": False, "error": error}

                data = response_json if isinstance(response_json, dict) else response.json()
                message = data.get("message", "")
                email = data.get("email")
                if not email and " for " in message:
                    email = message.split(" for ")[-1]

                return {
                    "success": True,
                    "action": data.get("action"),
                    "message": message,
                    "email": email,
                    "at_expires": data.get("at_expires"),
                }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def get_status(self) -> Dict[str, Any]:
        return {
            "total_sync_count": self._total_sync_count,
            "total_error_count": self._total_error_count,
            "last_batch_time": self._last_batch_time.isoformat() if self._last_batch_time else None,
            "flow2api_url": config.flow2api_url,
            "has_connection_token": bool(config.connection_token),
            "refresh_interval_minutes": config.refresh_interval,
        }


token_syncer = TokenSyncer()
