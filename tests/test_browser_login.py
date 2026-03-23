import unittest
from unittest.mock import AsyncMock, patch

from token_updater.browser import BrowserManager


class BrowserLoginHelperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = BrowserManager()

    def test_detect_login_blocker_handles_two_factor_prompt(self):
        message = self.manager._detect_login_blocker("2-Step Verification\nCheck your phone")
        self.assertEqual(message, "该账号需要人工完成二次验证，请改用手动登录")

    def test_resolve_known_email_prefers_page_text(self):
        profile = {
            "email": "",
            "login_account": "alias@example.com",
        }

        resolved = self.manager._resolve_known_email(profile, "Welcome real.user@example.com")

        self.assertEqual(resolved, "real.user@example.com")

    def test_resolve_known_email_falls_back_to_login_account(self):
        profile = {
            "email": "",
            "login_account": "Alias@Example.com",
        }

        resolved = self.manager._resolve_known_email(profile)

        self.assertEqual(resolved, "alias@example.com")

    async def test_handle_chromium_signin_prompt_handles_chromium_variant(self):
        click_button = AsyncMock(return_value=True)

        with patch.object(self.manager, "_click_button_by_text", click_button):
            result = await self.manager._handle_chromium_signin_prompt(
                AsyncMock(),
                "Sign in to Chromium? Set up a work profile",
            )

        self.assertTrue(result)
        self.assertIn("Continue as", click_button.await_args_list[0].args[1])

    async def test_handle_browser_settings_prompt_dismisses_default_browser(self):
        click_button = AsyncMock(return_value=True)

        with patch.object(self.manager, "_click_button_by_text", click_button):
            result = await self.manager._handle_browser_settings_prompts(
                AsyncMock(),
                "Make Chrome your default browser",
            )

        self.assertTrue(result)
        self.assertIn("Not now", click_button.await_args_list[0].args[1])

    async def test_auto_login_requires_credentials(self):
        profile = {
            "id": 1,
            "name": "alpha",
            "login_account": "",
            "login_password": "",
        }

        with patch("token_updater.browser.profile_db.get_profile", AsyncMock(return_value=profile)):
            result = await self.manager.auto_login(1)

        self.assertFalse(result["success"])
        self.assertIn("请先为该账号配置登录账号和登录密码", result["error"])

    async def test_persist_login_state_updates_email(self):
        update_profile = AsyncMock()

        with patch("token_updater.browser.profile_db.update_profile", update_profile):
            await self.manager._persist_login_state(1, "secret-token", email="Alpha@Example.com")

        update_profile.assert_awaited_once()
        kwargs = update_profile.await_args.kwargs
        self.assertEqual(kwargs["is_logged_in"], 1)
        self.assertEqual(kwargs["email"], "alpha@example.com")
        self.assertIn("last_token", kwargs)


if __name__ == "__main__":
    unittest.main()
