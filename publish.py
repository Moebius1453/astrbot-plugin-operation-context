import re


PREPARE_RULES = '''
THIS TASK IS PREPARATION ONLY
- Fill the post editor completely, then stop before pressing 发布.
- Do not tap 发布, 直接发布, 发送 or any other submit control. The host performs the final commit.
- Title and body come from frozen files. Open the files and use their content byte for byte.
- Report in your final sentence what is now in the editor and whether images or mentions were attached.
'''

DELETE_RULES = '''
THIS TASK IS A DELETION
- The post to remove is identified by the exact title in /tmp/operation-target.txt.
- Open your own profile, find the post whose title matches that file byte for byte, open its menu and delete it.
- If more than one post matches, stop and say so instead of guessing.
- Confirm the deletion dialog, then check the post is gone and report what you saw.
'''

PATH_PATTERN = re.compile(r'^/workspace/[A-Za-z0-9._-]+\.(png|jpg|jpeg)$')

IMAGES_ENABLED = False


def validate_publish(title, text, images, mention):
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise ValueError('invalid_post_title')
    if any(ord(character) < 32 and character not in '\n\t' for character in title):
        raise ValueError('invalid_post_title')
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('invalid_post_text')
    if any(ord(character) < 32 and character not in '\n\t' for character in text):
        raise ValueError('invalid_post_text')
    if images is not None:
        if not IMAGES_ENABLED:
            if images:
                raise ValueError('post_images_disabled')
        else:
            if not isinstance(images, list) or len(images) > 9:
                raise ValueError('invalid_post_images')
            for path in images:
                if not isinstance(path, str) or not PATH_PATTERN.fullmatch(path):
                    raise ValueError('invalid_post_image_path')
    if mention is not None and mention != '':
        if not isinstance(mention, str) or not mention.strip() or len(mention) > 50:
            raise ValueError('invalid_post_mention')
        if any(ord(character) < 32 for character in mention):
            raise ValueError('invalid_post_mention')


def build_goal(images, mention):
    parts = [
        'Open the post editor for a normal text post (type 内容) and prepare it, then stop.',
        'The title is in /tmp/operation-title.txt and the body in /tmp/operation-body.txt.',
        'Type them exactly; do not translate, shorten, add or remove punctuation.',
    ]
    if images:
        parts.append(
            'Attach these images, in this order: ' + ', '.join(images) + '. '
            'Each must be pushed to the phone gallery first with adb push to /sdcard/Pictures/ '
            'followed by a media scan, then picked in the image picker.'
        )
    if mention:
        parts.append(
            f'Mention {mention} inside the body: type a single @ and use the native mention picker, '
            'not a plain @text string.'
        )
    parts.append('When the editor shows the finished post, stop and describe what you see.')
    return ' '.join(parts)
