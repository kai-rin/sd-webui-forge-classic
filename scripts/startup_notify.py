"""Notify user when WebUI server is ready (Windows toast + sound)."""

import winsound

from modules import cmd_args, script_callbacks, shared


def notify_server_ready(_demo, _app):
    port = cmd_args.parser.parse_args().port or 7861
    url = f"http://127.0.0.1:{port}"

    # Sound notification
    winsound.MessageBeep(winsound.MB_ICONASTERISK)

    # Windows toast notification via PowerShell
    try:
        import subprocess

        title = "Stable Diffusion WebUI"
        message = f"Server ready at {url}"
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
            "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$textNodes = $template.GetElementsByTagName('text');"
            f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{title}')) > $null;"
            f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{message}')) > $null;"
            "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Stable Diffusion');"
            "$notifier.Show([Windows.UI.Notifications.ToastNotification]::new($template))"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # Toast is best-effort; sound already played


script_callbacks.on_app_started(notify_server_ready, name="startup_notify")
