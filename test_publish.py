import unittest

from .publish import DELETE_RULES, PREPARE_RULES, build_goal, validate_publish


class PublishRequestTests(unittest.TestCase):
    def test_accepts_plain_post_without_extras(self):
        validate_publish('标题', '正文', None, None)
        validate_publish('标题', '正文', [], '')

    def test_accepts_sandbox_images_and_mention(self):
        validate_publish('标题', '正文', ['/workspace/a.png', '/workspace/b.jpg'], 'cannot_view')

    def test_rejects_bad_title_and_body(self):
        for title, text in (('', '正文'), ('x' * 101, '正文'), ('标题', ''), ('标题', 'y' * 4001),
                            ('标题\x00', '正文'), ('标题', '正文\x07')):
            with self.assertRaises(ValueError):
                validate_publish(title, text, None, None)

    def test_rejects_unsafe_image_paths(self):
        for images in (['/etc/passwd'], ['/workspace/../../x.png'], ['/workspace/a.txt'],
                       ['a.png'], ['/workspace/a.png'] * 10, 'a.png'):
            with self.assertRaises(ValueError):
                validate_publish('标题', '正文', images, None)

    def test_rejects_control_characters_in_mention(self):
        for mention in ('x' * 51, 'a\x00b', '   '):
            with self.assertRaises(ValueError):
                validate_publish('标题', '正文', None, mention)


class GoalTests(unittest.TestCase):
    def test_goal_states_frozen_files_and_stop_rule(self):
        goal = build_goal(None, None)
        self.assertIn('/tmp/operation-title.txt', goal)
        self.assertIn('/tmp/operation-body.txt', goal)
        self.assertIn('stop', goal)

    def test_goal_mentions_images_and_native_picker(self):
        goal = build_goal(['/workspace/a.png'], 'cannot_view')
        self.assertIn('/workspace/a.png', goal)
        self.assertIn('media scan', goal)
        self.assertIn('cannot_view', goal)
        self.assertIn('native mention picker', goal)

    def test_rules_forbid_final_submit(self):
        self.assertIn('stop before pressing', PREPARE_RULES)
        self.assertIn('Do not tap', PREPARE_RULES)
        self.assertIn('operation-target.txt', DELETE_RULES)
