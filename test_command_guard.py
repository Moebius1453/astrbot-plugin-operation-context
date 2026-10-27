import unittest

from .command_guard import count_device_actions, guard


ADB = '/workspace/.local/adbdl/platform-tools/adb -s 172.22.0.2:5555 '


class CommandGuardTests(unittest.TestCase):
    def test_normal_observation_and_navigation_pass(self):
        for command in (
            ADB + 'shell uiautomator dump --compressed /sdcard/ui.xml',
            ADB + 'shell cat /sdcard/ui.xml',
            ADB + 'shell input tap 360 900',
            ADB + 'shell input swipe 360 900 360 400 300',
            ADB + 'shell input keyevent KEYCODE_BACK',
            ADB + 'shell dumpsys window | grep mCurrentFocus',
            ADB + 'exec-out screencap -p > /workspace/shot-1.png',
            ADB + 'shell am force-stop com.max.xiaoheihe',
            ADB + 'shell am start -n com.max.xiaoheihe/.MainActivity',
            'rm -rf /tmp/operation-work',
            'base64 -w0 /workspace/shot-1.png',
        ):
            self.assertIsNone(guard(command), command)

    def test_only_the_input_broadcast_is_allowed(self):
        self.assertIsNone(guard(
            ADB + 'shell am broadcast -a ADB_INPUT_B64 -p com.android.adbkeyboard --es msg aGk='
        ))
        self.assertEqual(guard(ADB + 'shell am broadcast -a com.example.PING'), 'broadcast_not_input')
        self.assertEqual(guard(ADB + 'shell am broadcast -p com.example'), 'broadcast_without_explicit_action')
        self.assertEqual(guard(
            ADB + 'shell am broadcast -a ADB_INPUT_B64 ; ' + ADB + 'shell am broadcast -a SEND'
        ), 'broadcast_not_input')

    def test_destructive_device_commands_are_blocked(self):
        for command, reason in (
            (ADB + 'shell pm clear com.max.xiaoheihe', 'package_mutation'),
            (ADB + 'shell pm uninstall com.max.xiaoheihe', 'package_mutation'),
            (ADB + 'uninstall com.max.xiaoheihe', 'package_mutation'),
            (ADB + 'shell settings put secure default_input_method com.example', 'system_setting_mutation'),
            (ADB + 'shell settings delete global adb_enabled', 'system_setting_mutation'),
            (ADB + 'shell wm size 1080x1920', 'display_mutation'),
            (ADB + 'reboot', 'device_power'),
            (ADB + 'shell svc wifi disable', 'radio_mutation'),
        ):
            self.assertEqual(guard(command), reason, command)

    def test_foreign_device_is_blocked(self):
        self.assertEqual(
            guard('/workspace/.local/adbdl/platform-tools/adb -s 10.0.0.9:5555 shell input tap 1 1'),
            'foreign_device',
        )

    def test_qq_is_blocked_anywhere_in_the_command(self):
        for command in (
            ADB + 'shell monkey -p com.tencent.mobileqq 1',
            'adb -s 172.22.0.2:5555 shell dumpsys activity com.tencent.tim',
        ):
            self.assertEqual(guard(command), 'qq_forbidden', command)

    def test_recursive_delete_outside_tmp_is_blocked(self):
        for command in (
            'rm -rf /workspace/shiro',
            'rm -rf /sdcard/Pictures',
            ADB + 'shell rm -rf /sdcard',
            'rm -r -f /workspace',
        ):
            self.assertEqual(guard(command), 'recursive_delete_outside_tmp', command)
        self.assertIsNone(guard('rm -rf /tmp/build'))
        self.assertIsNone(guard('rm -f /workspace/shot-1.png'))

    def test_sandbox_mutations_are_blocked(self):
        for command, reason in (
            ('pip install requests', 'package_install'),
            ('sudo apt-get install -y ffmpeg', 'package_install'),
            ('npm install whatever', 'package_install'),
            ('systemctl start nginx', 'background_process'),
            ('nohup python3 server.py &', 'background_process'),
            ('dd if=/dev/zero of=/workspace/big', 'disk_mutation'),
            (ADB + 'push payload /system/bin/x', 'system_partition_write'),
        ):
            self.assertEqual(guard(command), reason, command)

    def test_non_string_is_rejected(self):
        self.assertEqual(guard(None), 'invalid_command')

    def test_device_action_count(self):
        self.assertEqual(count_device_actions(ADB + 'shell input tap 1 1'), 1)
        self.assertEqual(count_device_actions(
            ADB + 'shell input tap 1 1 && ' + ADB + 'shell input swipe 1 1 2 2 300'
        ), 2)
        self.assertEqual(count_device_actions(ADB + 'shell dumpsys window'), 0)
        self.assertEqual(count_device_actions(None), 0)


if __name__ == '__main__':
    unittest.main()
