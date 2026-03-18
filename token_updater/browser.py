"""浏览器管理 v3.1 - 持久化上下文 + VNC登录 + Headless刷新"""
import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, BrowserContext, Playwright
from .config import config
from .database import profile_db
from .proxy_utils import parse_proxy, format_proxy_for_playwright
from .logger import logger


# 内存优化参数
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--disable-features=TranslateUI",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--max_old_space_size=128",  # 限制 V8 内存
    "--js-flags=--max-old-space-size=128",
]

LOGIN_BROWSER_ARGS = BROWSER_ARGS[:6] + ["--disable-blink-features=AutomationControlled"]

BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

SUPERVISOR_CONF = "/etc/supervisor/conf.d/supervisord.conf"
VNC_START_ORDER = ("xvfb", "fluxbox", "x11vnc", "novnc")
VNC_STOP_ORDER = ("novnc", "x11vnc", "fluxbox", "xvfb")


class BrowserManager:
    """浏览器管理器 - 持久化上下文"""

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._active_context: Optional[BrowserContext] = None
        self._active_profile_id: Optional[int] = None
        self._lock = asyncio.Lock()

    async def start(self):
        """启动 Playwright"""
        if self._playwright:
            return
        logger.info("启动 Playwright...")
        self._playwright = await async_playwright().start()
        os.makedirs(config.profiles_dir, exist_ok=True)
        logger.info("Playwright 已启动")

    async def stop(self):
        """停止"""
        await self._close_active()
        await self._stop_vnc_stack()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    def _supervisorctl(self, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
        exe = shutil.which("supervisorctl")
        if not exe:
            raise RuntimeError("supervisorctl not found")
        cmd = [exe, "-c", SUPERVISOR_CONF, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)

    def _get_supervisor_status(self) -> Dict[str, str]:
        try:
            cp = self._supervisorctl("status", timeout=8.0)
        except Exception:
            return {}

        status: Dict[str, str] = {}
        for line in (cp.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                status[parts[0]] = parts[1]
        return status

    async def _ensure_vnc_stack(self) -> bool:
        if not config.enable_vnc:
            return False

        status = self._get_supervisor_status()
        for prog in VNC_START_ORDER:
            if status.get(prog) == "RUNNING":
                continue
            try:
                cp = self._supervisorctl("start", prog, timeout=20.0)
                if cp.returncode != 0:
                    logger.warning(f"启动 {prog} 失败: {(cp.stdout or '').strip()} {(cp.stderr or '').strip()}")
                    return False
            except Exception as e:
                logger.warning(f"启动 {prog} 异常: {e}")
                return False

            if prog == "xvfb":
                await asyncio.sleep(0.4)

        return True

    async def _stop_vnc_stack(self) -> None:
        if not config.enable_vnc:
            return

        for prog in VNC_STOP_ORDER:
            try:
                self._supervisorctl("stop", prog, timeout=10.0)
            except Exception:
                pass

    async def _close_active(self):
        """关闭当前浏览器"""
        if self._active_context:
            try:
                await self._active_context.close()
            except Exception:
                pass
            self._active_context = None
            self._active_profile_id = None
            logger.info("浏览器已关闭")

    def _get_profile_dir(self, profile_id: int) -> str:
        """获取 Profile 持久化目录"""
        return os.path.join(os.path.abspath(config.profiles_dir), f"profile_{profile_id}")

    def _clean_locks(self, profile_dir: str):
        """清理 Chromium 锁文件"""
        lock_files = ["SingletonLock", "SingletonCookie", "SingletonSocket"]
        for lock in lock_files:
            lock_path = os.path.join(profile_dir, lock)
            if os.path.exists(lock_path):
                try:
                    os.remove(lock_path)
                    logger.info(f"已清理锁文件: {lock}")
                except Exception:
                    pass

    def _mask_token(self, token: str) -> str:
        if not token or len(token) <= 8:
            return token or ""
        return f"{token[:4]}...{token[-4:]}"

    async def _get_proxy(self, profile: Dict[str, Any]) -> Optional[Dict]:
        """获取代理配置"""
        if profile.get("proxy_enabled") and profile.get("proxy_url"):
            proxy_config = parse_proxy(profile["proxy_url"])
            if proxy_config:
                proxy = format_proxy_for_playwright(proxy_config)
                logger.info(f"[{profile['name']}] 使用代理: {proxy['server']}")
                return proxy
        return None

    async def _launch_persistent_context(self, profile: Dict[str, Any], profile_dir: str, *, headless: bool, proxy: Optional[Dict], login_mode: bool = False) -> BrowserContext:
        """统一的持久化上下文启动器，失败时自动回退一次。"""
        base_args = LOGIN_BROWSER_ARGS if login_mode else BROWSER_ARGS
        launch_attempts = [
            (base_args, "default"),
            ([arg for arg in base_args if arg != "--disable-dev-shm-usage"], "no-disable-dev-shm-usage"),
        ]

        last_error: Optional[Exception] = None
        for args, label in launch_attempts:
            try:
                logger.info(f"[{profile['name']}] 启动浏览器上下文 ({label}, headless={headless})")
                return await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=headless,
                    viewport={"width": 1600, "height": 900},
                    locale="en-US",
                    timezone_id="America/New_York",
                    proxy=proxy,
                    args=args,
                    ignore_default_args=["--enable-automation"],
                )
            except Exception as e:
                last_error = e
                logger.warning(f"[{profile['name']}] 启动浏览器上下文失败 ({label}): {e}")
                await asyncio.sleep(0.8)

        if last_error:
            raise last_error
        raise RuntimeError("launch_persistent_context failed without error")

    def _parse_cookies_payload(self, cookies_json: str) -> List[Dict[str, Any]]:
        data = json.loads(cookies_json)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            cookies = data.get("cookies")
            if isinstance(cookies, list):
                return cookies
        return []

    def _to_playwright_cookies(self, cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in cookies:
            if not isinstance(c, dict):
                continue

            name = c.get("name")
            value = c.get("value")
            if not name or value is None:
                continue

            domain = c.get("domain") or c.get("host")
            url = c.get("url")
            path = c.get("path") or "/"

            if isinstance(domain, str) and "://" in domain:
                domain = None

            cookie: Dict[str, Any] = {"name": str(name), "value": str(value)}

            if c.get("httpOnly") is not None:
                cookie["httpOnly"] = bool(c.get("httpOnly"))
            if c.get("secure") is not None:
                cookie["secure"] = bool(c.get("secure"))

            expires = c.get("expires")
            if expires is None:
                expires = c.get("expirationDate") or c.get("expiry")
            if expires is not None:
                try:
                    cookie["expires"] = float(expires)
                except (TypeError, ValueError):
                    pass

            same_site = c.get("sameSite")
            if isinstance(same_site, str):
                m = same_site.strip().lower()
                if m in {"lax"}:
                    cookie["sameSite"] = "Lax"
                elif m in {"strict"}:
                    cookie["sameSite"] = "Strict"
                elif m in {"none", "no_restriction"}:
                    cookie["sameSite"] = "None"

            if isinstance(url, str) and url.startswith("http"):
                cookie["url"] = url
            elif isinstance(domain, str) and domain:
                cookie["domain"] = domain
                cookie["path"] = str(path)
            else:
                continue

            out.append(cookie)
        return out

    async def _get_session_cookie(self, context: BrowserContext) -> Optional[str]:
        try:
            cookies = await context.cookies("https://labs.google")
        except Exception:
            cookies = await context.cookies()

        for cookie in cookies:
            if cookie.get("name") == config.session_cookie_name:
                return cookie.get("value")
        return None

    async def import_cookies(self, profile_id: int, cookies_json: str) -> Dict[str, Any]:
        """导入 Cookie（JSON），写入到持久化 profile 中"""
        if len(cookies_json) > 300_000:
            return {"success": False, "error": "Cookie 内容过大（建议只导出 labs.google 域名的 Cookie）"}

        async with self._lock:
            profile = await profile_db.get_profile(profile_id)
            if not profile:
                return {"success": False, "error": "Profile 不存在"}

            try:
                raw = self._parse_cookies_payload(cookies_json)
            except Exception as e:
                return {"success": False, "error": f"Cookie JSON 解析失败: {e}"}

            if not raw:
                return {"success": False, "error": "未识别到 Cookie 列表（请粘贴 JSON 数组或包含 cookies 字段的对象）"}

            cookies = self._to_playwright_cookies(raw)
            if not cookies:
                return {"success": False, "error": "Cookie 列表为空或格式不支持（至少需要 name/value/domain+path 或 url）"}

            context = None
            try:
                if not self._playwright:
                    await self.start()

                profile_dir = self._get_profile_dir(profile_id)
                os.makedirs(profile_dir, exist_ok=True)
                self._clean_locks(profile_dir)
                proxy = await self._get_proxy(profile)

                context = await self._launch_persistent_context(
                    profile,
                    profile_dir,
                    headless=True,
                    proxy=proxy,
                )

                await context.add_cookies(cookies)
                token = await self._get_session_cookie(context)

                await profile_db.update_profile(
                    profile_id,
                    is_logged_in=1 if token else 0,
                    last_token=self._mask_token(token) if token else None,
                    last_token_time=datetime.now().isoformat() if token else None,
                )

                return {
                    "success": True,
                    "imported": len(cookies),
                    "raw_count": len(raw),
                    "has_token": bool(token),
                }

            except Exception as e:
                logger.error(f"[{profile['name']}] Cookie 导入失败: {e}")
                return {"success": False, "error": str(e)}
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass

    async def launch_for_login(self, profile_id: int) -> bool:
        """启动浏览器用于 VNC 登录（非 headless）"""
        if not config.enable_vnc:
            logger.warning("已禁用 VNC 登录（设置 ENABLE_VNC=1 可启用）")
            return False
        async with self._lock:
            await self._close_active()

            profile = await profile_db.get_profile(profile_id)
            if not profile:
                logger.error(f"Profile {profile_id} 不存在")
                return False

            try:
                if not self._playwright:
                    await self.start()

                ok = await self._ensure_vnc_stack()
                if not ok:
                    logger.error(f"[{profile['name']}] VNC 服务启动失败")
                    return False

                profile_dir = self._get_profile_dir(profile_id)
                os.makedirs(profile_dir, exist_ok=True)
                self._clean_locks(profile_dir)  # 清理锁文件
                proxy = await self._get_proxy(profile)

                # 非 headless，用于 VNC 登录
                self._active_context = await self._launch_persistent_context(
                    profile,
                    profile_dir,
                    headless=False,
                    proxy=proxy,
                    login_mode=True,
                )
                self._active_profile_id = profile_id

                page = self._active_context.pages[0] if self._active_context.pages else await self._active_context.new_page()
                await page.goto(config.labs_url, wait_until="domcontentloaded")

                logger.info(f"[{profile['name']}] 浏览器已启动，请通过 VNC 登录")
                return True

            except Exception as e:
                logger.error(f"[{profile['name']}] 启动失败: {e}")
                return False

    async def activate_session(self, profile_id: int, wait_seconds: float = 8.0) -> Dict[str, Any]:
        """自动做一次 headed 会话激活，等价于“点登录 -> 浏览器起来 -> 直接关闭”。"""
        if not config.enable_vnc:
            return {"success": False, "error": "VNC 未启用，无法执行 headed 会话激活"}

        async with self._lock:
            await self._close_active()
            profile = await profile_db.get_profile(profile_id)
            if not profile:
                return {"success": False, "error": "Profile 不存在"}

            context = None
            try:
                if not self._playwright:
                    await self.start()
                ok = await self._ensure_vnc_stack()
                if not ok:
                    return {"success": False, "error": "VNC 服务启动失败"}

                profile_dir = self._get_profile_dir(profile_id)
                os.makedirs(profile_dir, exist_ok=True)
                self._clean_locks(profile_dir)
                proxy = await self._get_proxy(profile)

                previous_token = None
                try:
                    previous_token = await self._peek_token_no_lock(profile_id)
                except Exception:
                    previous_token = None

                context = await self._launch_persistent_context(
                    profile,
                    profile_dir,
                    headless=False,
                    proxy=proxy,
                    login_mode=True,
                )
                page = context.pages[0] if context.pages else await context.new_page()
                try:
                    await page.goto(config.labs_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    try:
                        await page.goto(config.login_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass

                deadline = asyncio.get_running_loop().time() + max(wait_seconds, 3.0)
                token = previous_token
                changed = False
                while asyncio.get_running_loop().time() < deadline:
                    current_token = await self._get_session_cookie(context)
                    if current_token:
                        token = current_token
                        if previous_token is None or current_token != previous_token:
                            changed = True
                            break
                    await asyncio.sleep(0.5)

                if token:
                    await profile_db.update_profile(
                        profile_id,
                        is_logged_in=1,
                        last_token=self._mask_token(token),
                        last_token_time=datetime.now().isoformat(),
                    )
                else:
                    await profile_db.update_profile(profile_id, is_logged_in=0)

                return {
                    "success": bool(token),
                    "token": token,
                    "changed": changed,
                    "had_previous": bool(previous_token),
                    "error": None if token else "未读取到 session token",
                }
            except Exception as e:
                logger.error(f"[{profile['name']}] headed 会话激活失败: {e}")
                return {"success": False, "error": str(e)}
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                await self._stop_vnc_stack()

    async def recover_session_via_login_cycle(self, profile_id: int, settle_seconds: float = 5.0) -> Dict[str, Any]:
        """模拟人工恢复流程：点击登录拉起浏览器 -> 等待浏览器稳定 -> 关闭浏览器 -> 再提取 token。"""
        if not config.enable_vnc:
            return {"success": False, "error": "VNC 未启用，无法执行浏览器登录恢复流程"}

        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        previous_token = None
        try:
            previous_token = await self.peek_token(profile_id)
        except Exception:
            previous_token = None

        launched = await self.launch_for_login(profile_id)
        if not launched:
            return {"success": False, "error": "启动登录浏览器失败"}

        try:
            await asyncio.sleep(max(settle_seconds, 2.0))
        finally:
            close_result = await self.close_browser(profile_id)
            if not close_result.get("success"):
                logger.warning(f"[{profile['name']}] 自动关闭登录浏览器失败: {close_result.get('error')}")

        token = await self.extract_token(profile_id)
        changed = bool(token and (previous_token is None or token != previous_token))

        if token:
            return {
                "success": True,
                "token": token,
                "changed": changed,
                "had_previous": bool(previous_token),
                "close_result": close_result,
            }

        return {
            "success": False,
            "error": "登录浏览器启停恢复后仍无法提取 token",
            "changed": False,
            "had_previous": bool(previous_token),
            "close_result": close_result,
        }

    async def close_browser(self, profile_id: int) -> Dict[str, Any]:
        """关闭浏览器并保存状态"""
        async with self._lock:
            if self._active_profile_id != profile_id:
                return {"success": False, "error": "该 Profile 浏览器未运行"}

            if self._active_context:
                # 检查登录状态
                is_logged_in = False
                try:
                    cookies = await self._active_context.cookies("https://labs.google")
                    is_logged_in = any(c["name"] == config.session_cookie_name for c in cookies)
                except Exception:
                    pass

                await profile_db.update_profile(profile_id, is_logged_in=int(is_logged_in))
                await self._close_active()
                await self._stop_vnc_stack()

                status = "已登录" if is_logged_in else "未登录"
                logger.info(f"Profile {profile_id} 浏览器已关闭，状态: {status}")
                return {"success": True, "is_logged_in": is_logged_in}

            return {"success": True}

    async def extract_token(self, profile_id: int) -> Optional[str]:
        """提取 Token（Headless 模式，使用持久化上下文）"""
        async with self._lock:
            profile = await profile_db.get_profile(profile_id)
            if not profile:
                return None

            profile_dir = self._get_profile_dir(profile_id)

            # 检查是否有持久化数据
            if not os.path.exists(profile_dir):
                logger.warning(f"[{profile['name']}] 无持久化数据，请先登录")
                return None

            previous_token = None
            try:
                previous_token = await self._peek_token_no_lock(profile_id)
            except Exception:
                previous_token = None

            # 如果当前 profile 浏览器正在运行（VNC 登录中），直接提取
            if self._active_profile_id == profile_id and self._active_context:
                token = await self._extract_from_context(profile, self._active_context)
                if token and previous_token and token == previous_token:
                    logger.warning(f"[{profile['name']}] 提取到的 session token 与刷新前一致，疑似仍为旧会话")
                return token

            # 否则用 headless 模式启动
            context = None
            try:
                if not self._playwright:
                    await self.start()

                profile_dir = self._get_profile_dir(profile_id)
                self._clean_locks(profile_dir)  # 清理锁文件
                proxy = await self._get_proxy(profile)

                logger.info(f"[{profile['name']}] Headless 模式提取 Token...")

                # Headless + 持久化上下文
                context = await self._launch_persistent_context(
                    profile,
                    profile_dir,
                    headless=True,
                    proxy=proxy,
                )

                token = await self._extract_from_context(profile, context)
                if token and previous_token and token == previous_token:
                    logger.warning(f"[{profile['name']}] 提取到的 session token 与刷新前一致，疑似仍为旧会话")
                return token

            except Exception as e:
                logger.error(f"[{profile['name']}] 提取失败: {e}")
                return None
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                    logger.info(f"[{profile['name']}] Headless 浏览器已关闭")

    async def _extract_from_context(self, profile: Dict[str, Any], context: BrowserContext) -> Optional[str]:
        """从上下文提取 Token（通过真正的登录入口刷新 session）"""
        page = None
        try:
            page = await context.new_page()

            async def _route(route, request):
                try:
                    if request.resource_type in BLOCKED_RESOURCE_TYPES:
                        await route.abort()
                    else:
                        await route.continue_()
                except Exception:
                    try:
                        await route.continue_()
                    except Exception:
                        pass

            try:
                await page.route("**/*", _route)
            except Exception:
                pass

            previous_token = await self._get_session_cookie(context)
            if previous_token:
                logger.info(f"[{profile['name']}] 刷新前 session token: {self._mask_token(previous_token)}")
            else:
                logger.info(f"[{profile['name']}] 刷新前未读取到 session token")

            logger.info(f"[{profile['name']}] 访问 {config.login_url} 刷新 session...")
            await page.goto(config.login_url, wait_until="domcontentloaded", timeout=60000)

            try:
                await page.wait_for_url("https://labs.google/**", timeout=30000)
                logger.info(f"[{profile['name']}] 已成功跳转到 labs.google")
            except Exception as e:
                logger.warning(f"[{profile['name']}] 等待跳转超时: {e}")
                try:
                    await page.goto(config.labs_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as goto_err:
                    logger.warning(f"[{profile['name']}] 回退访问 {config.labs_url} 失败: {goto_err}")

            token = previous_token
            changed = False
            deadline = asyncio.get_running_loop().time() + 20.0
            while asyncio.get_running_loop().time() < deadline:
                current_token = await self._get_session_cookie(context)
                if current_token:
                    token = current_token
                    if previous_token is None or current_token != previous_token:
                        changed = True
                        break
                await asyncio.sleep(0.5)

            if not changed:
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                current_token = await self._get_session_cookie(context)
                if current_token:
                    token = current_token
                    changed = previous_token is None or current_token != previous_token

            if token:
                await profile_db.update_profile(
                    profile["id"],
                    is_logged_in=1,
                    last_token=self._mask_token(token),
                    last_token_time=datetime.now().isoformat(),
                )
                if changed:
                    logger.info(f"[{profile['name']}] Token 提取成功，session 已刷新")
                else:
                    logger.warning(f"[{profile['name']}] Token 提取成功，但 session token 未变化")
            else:
                await profile_db.update_profile(profile["id"], is_logged_in=0)
                logger.warning(f"[{profile['name']}] 未找到 Token，会话可能已过期")

            return token

        except Exception as e:
            logger.error(f"[{profile['name']}] 提取异常: {e}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def check_login_status(self, profile_id: int) -> Dict[str, Any]:
        """检查登录状态"""
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return {"success": False, "error": "Profile 不存在"}

        token = await self.peek_token(profile_id)
        await profile_db.update_profile(profile_id, is_logged_in=1 if token else 0)
        return {
            "success": True,
            "is_logged_in": token is not None,
            "profile_name": profile["name"]
        }

    async def _peek_token_no_lock(self, profile_id: int) -> Optional[str]:
        """轻量获取 token（不访问页面，仅读取 cookie）；调用方负责锁。"""
        profile = await profile_db.get_profile(profile_id)
        if not profile:
            return None

        profile_dir = self._get_profile_dir(profile_id)
        if not os.path.exists(profile_dir):
            return None

        if self._active_profile_id == profile_id and self._active_context:
            return await self._get_session_cookie(self._active_context)

        context = None
        try:
            if not self._playwright:
                await self.start()

            self._clean_locks(profile_dir)
            proxy = await self._get_proxy(profile)
            context = await self._launch_persistent_context(
                profile,
                profile_dir,
                headless=True,
                proxy=proxy,
            )
            return await self._get_session_cookie(context)
        except Exception:
            return None
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

    async def peek_token(self, profile_id: int) -> Optional[str]:
        """轻量获取 token（不访问页面，仅读取 cookie）"""
        async with self._lock:
            return await self._peek_token_no_lock(profile_id)

    async def delete_profile_data(self, profile_id: int):
        """删除 profile 数据"""
        profile_dir = self._get_profile_dir(profile_id)
        if os.path.exists(profile_dir):
            shutil.rmtree(profile_dir)
            logger.info(f"已删除: {profile_dir}")

    def get_active_profile_id(self) -> Optional[int]:
        return self._active_profile_id

    def get_status(self) -> Dict[str, Any]:
        status = self._get_supervisor_status()
        vnc_stack_running = all(status.get(p) == "RUNNING" for p in ("xvfb", "x11vnc", "novnc")) if status else False
        return {
            "is_running": self._playwright is not None,
            "active_profile_id": self._active_profile_id,
            "has_active_browser": self._active_context is not None,
            "profiles_dir": config.profiles_dir,
            "enable_vnc": bool(config.enable_vnc),
            "vnc_stack_running": bool(vnc_stack_running),
        }


browser_manager = BrowserManager()
