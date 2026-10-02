import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_intake.core import connect, enqueue
from media_intake.kindle import (deliver_to_kindle, maybe_auto_send_to_kindle,
                                get_kindle_email_targets,
                                pending_kindle_delivery_ids,
                                retry_pending_kindle_deliveries, set_setting)


class KindleDeliveryTest(unittest.TestCase):
    def _downloaded_book(self, db, folder, extension='.azw3'):
        source = Path(folder) / f'source{extension}'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'original ebook bytes')
        queued = enqueue(db, {
            'title': 'Example Book', 'author': 'Example Writer',
            'url': f'https://books.example/{source.name}', 'source': 'test fixture',
            'format': extension, 'kind': 'book', 'category': 'Books',
        })
        db.execute("UPDATE items SET state='downloaded',file_path=? WHERE id=?",
                   (str(source.resolve()), queued['id']))
        db.commit()
        return queued['id'], source

    def test_copies_supported_book_and_verifies_bytes(self):
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            documents = Path(temp) / 'Kindle' / 'documents'
            documents.mkdir(parents=True)
            item_id, source = self._downloaded_book(db, Path(temp) / 'downloads')
            set_setting(db, 'kindle_device_path', str(documents))

            result = deliver_to_kindle(db, item_id, temp, force=True)

            details = json.loads(result['metadata'])['kindle_delivery']
            target = Path(details['path'])
            self.assertEqual(details['status'], 'arrived')
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(target.suffix, '.azw3')
            self.assertTrue(source.is_file())

    def test_converts_epub_to_azw3_with_configured_kindling_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            documents = Path(temp) / 'Kindle' / 'documents'
            documents.mkdir(parents=True)
            converter = Path(temp) / 'kindling-cli.exe'
            converter.write_bytes(b'test placeholder')
            item_id, source = self._downloaded_book(db, Path(temp) / 'downloads', '.epub')
            set_setting(db, 'kindle_device_path', str(documents))
            set_setting(db, 'kindle_converter_path', str(converter))
            set_setting(db, 'kindle_target_format', 'AZW3')

            def fake_convert(command, **_kwargs):
                Path(command[4]).write_bytes(b'converted AZW3 bytes')
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with patch('media_intake.kindle.subprocess.run', side_effect=fake_convert) as run:
                result = deliver_to_kindle(db, item_id, temp, force=True)

            details = json.loads(result['metadata'])['kindle_delivery']
            self.assertEqual(details['status'], 'arrived')
            self.assertEqual(Path(details['path']).read_bytes(), b'converted AZW3 bytes')
            self.assertEqual(Path(details['path']).suffix, '.azw3')
            command = run.call_args.args[0]
            self.assertEqual(command[:4], [str(converter), 'build',
                                           str(source.resolve()), '--output'])
            self.assertEqual(Path(command[4]).suffix, '.azw3')
            self.assertTrue(source.is_file())

    def test_auto_delivery_waits_then_retries_when_device_is_connected(self):
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            item_id, _source = self._downloaded_book(db, Path(temp) / 'downloads')
            set_setting(db, 'kindle_auto_send', '1')

            with patch('media_intake.kindle.find_kindle_documents_folder', return_value=None):
                waiting = maybe_auto_send_to_kindle(db, item_id, temp)

            waiting_details = json.loads(waiting['metadata'])['kindle_delivery']
            self.assertEqual(waiting_details['status'], 'waiting_for_device')
            self.assertEqual(pending_kindle_delivery_ids(db), [item_id])

            documents = Path(temp) / 'Kindle' / 'documents'
            documents.mkdir(parents=True)
            set_setting(db, 'kindle_device_path', str(documents))
            delivered = retry_pending_kindle_deliveries(db, temp)

            self.assertEqual(len(delivered), 1)
            self.assertEqual(json.loads(delivered[0]['metadata'])['kindle_delivery']['status'],
                             'arrived')
            self.assertEqual(pending_kindle_delivery_ids(db), [])

    def test_non_book_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            queued = enqueue(db, {
                'title': 'Episode', 'url': 'https://video.example/episode.mp4',
                'source': 'test fixture', 'format': '.mp4', 'kind': 'video',
                'category': 'Videos',
            })
            media_file = Path(temp) / 'episode.mp4'
            media_file.write_bytes(b'video')
            db.execute("UPDATE items SET state='downloaded',file_path=? WHERE id=?",
                       (str(media_file), queued['id']))
            db.commit()

            with self.assertRaisesRegex(ValueError, 'not a supported book'):
                deliver_to_kindle(db, queued['id'], temp, force=True)

    def test_legacy_kindle_email_is_migrated_to_one_named_destination(self):
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            set_setting(db, 'kindle_email_to', 'reader@example.com')

            self.assertEqual(get_kindle_email_targets(db), [
                {'name': 'Kindle 1', 'email': 'reader@example.com'}])

    def test_email_delivery_sends_separate_messages_to_each_kindle(self):
        class FakeSMTP:
            def __init__(self):
                self.messages = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def ehlo(self):
                return 250, b'OK'

            def starttls(self, **_kwargs):
                return 220, b'OK'

            def login(self, *_args):
                return 235, b'OK'

            def send_message(self, message, **_kwargs):
                self.messages.append(message)
                return {}

        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            item_id, _source = self._downloaded_book(
                db, Path(temp) / 'downloads', '.epub')
            set_setting(db, 'kindle_delivery_method', 'EMAIL')
            set_setting(db, 'kindle_email_targets', json.dumps([
                {'name': 'Paperwhite', 'email': 'paperwhite@example.com'},
                {'name': 'Oasis', 'email': 'oasis@example.com'},
            ]))
            set_setting(db, 'kindle_smtp_host', 'smtp.example.com')
            set_setting(db, 'kindle_smtp_port', '587')
            set_setting(db, 'kindle_smtp_security', 'STARTTLS')
            set_setting(db, 'kindle_smtp_from', 'sender@example.com')
            set_setting(db, 'kindle_smtp_username', 'sender@example.com')
            server = FakeSMTP()

            with patch('media_intake.kindle.smtplib.SMTP', return_value=server), \
                    patch('media_intake.kindle.get_kindle_smtp_password',
                          return_value='test-password'):
                result = deliver_to_kindle(db, item_id, temp, force=True)

            details = json.loads(result['metadata'])['kindle_delivery']
            self.assertEqual(details['status'], 'sent_unconfirmed')
            self.assertEqual(details['format'], 'EPUB')
            self.assertEqual([message['To'] for message in server.messages], [
                'paperwhite@example.com', 'oasis@example.com'])
            self.assertTrue(all(next(message.iter_attachments()).get_filename().endswith('.epub')
                                for message in server.messages))
            self.assertEqual([target['status'] for target in details['targets']], [
                'sent_unconfirmed', 'sent_unconfirmed'])
            self.assertEqual([target['name'] for target in details['targets']], [
                'Paperwhite', 'Oasis'])

    def test_email_can_experimentally_convert_epub_to_azw3(self):
        class FakeSMTP:
            def __init__(self):
                self.messages = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def ehlo(self):
                return 250, b'OK'

            def starttls(self, **_kwargs):
                return 220, b'OK'

            def send_message(self, message, **_kwargs):
                self.messages.append(message)
                return {}

        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            item_id, source = self._downloaded_book(
                db, Path(temp) / 'downloads', '.epub')
            converter = Path(temp) / 'kindling-cli.exe'
            converter.write_bytes(b'test placeholder')
            set_setting(db, 'kindle_delivery_method', 'EMAIL')
            set_setting(db, 'kindle_email_to', 'reader@example.com')
            set_setting(db, 'kindle_email_format', 'AZW3')
            set_setting(db, 'kindle_converter_path', str(converter))
            set_setting(db, 'kindle_smtp_host', 'smtp.example.com')
            set_setting(db, 'kindle_smtp_from', 'sender@example.com')
            server = FakeSMTP()

            def fake_convert(command, **_kwargs):
                Path(command[4]).write_bytes(b'converted AZW3 bytes')
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with patch('media_intake.kindle.subprocess.run', side_effect=fake_convert) as run, \
                    patch('media_intake.kindle.smtplib.SMTP', return_value=server):
                result = deliver_to_kindle(db, item_id, temp, force=True)

            details = json.loads(result['metadata'])['kindle_delivery']
            self.assertEqual(details['status'], 'sent_unconfirmed')
            self.assertEqual(details['format'], 'AZW3')
            attachment = next(server.messages[0].iter_attachments())
            self.assertTrue(attachment.get_filename().endswith('.azw3'))
            self.assertEqual(attachment.get_payload(decode=True), b'converted AZW3 bytes')
            self.assertEqual(run.call_args.args[0][:4], [
                str(converter), 'build', str(source.resolve()), '--output'])
            self.assertTrue(source.is_file())


if __name__ == '__main__':
    unittest.main()
