import io
from unittest import mock

from abstract import ViURTestCase


def png(width, height):
    import PIL.Image
    buffer = io.BytesIO()
    PIL.Image.new("RGB", (width, height), "red").save(buffer, "PNG")
    return buffer.getvalue()


class TestThumbnailerNeverEnlarges(ViURTestCase):
    """`thumbnailer` with a fixed width must only shrink; a narrower image keeps its size."""

    def _thumbnail(self, source):
        with mock.patch("google.cloud.storage.Client"):
            from viur.core.modules import file as file_module
        from viur.core import conf

        uploaded = {}
        source_blob = mock.Mock()
        source_blob.download_to_file.side_effect = lambda target: target.write(source)

        def blob(path):
            target = mock.Mock()
            target.upload_from_file.side_effect = lambda data, content_type: uploaded.update({path: data.read()})
            return target

        bucket = mock.Mock(get_blob=mock.Mock(return_value=source_blob), blob=blob)
        with mock.patch.object(conf, "main_app", mock.Mock(file=mock.Mock(get_bucket=lambda dlkey: bucket))):
            result = file_module.thumbnailer({"dlkey": "key", "name": "image.png"}, {}, [{"width": 480}])
        return result, uploaded

    def test_large_image_is_shrunk(self):
        result, uploaded = self._thumbnail(png(800, 400))
        name, _, mimetype, custom = result[0]
        self.assertEqual("thumbnail-w480.webp", name)
        self.assertEqual({"mimetype": "image/webp", "width": 480, "height": 240}, custom)
        self.assertIn("key/derived/thumbnail-w480.webp", uploaded)

    def test_small_image_is_not_enlarged(self):
        import PIL.Image
        result, uploaded = self._thumbnail(png(3, 2))
        name, _, _, custom = result[0]
        self.assertEqual("thumbnail-w3.webp", name)
        self.assertEqual((3, 2), (custom["width"], custom["height"]))
        with PIL.Image.open(io.BytesIO(uploaded["key/derived/thumbnail-w3.webp"])) as thumb:
            self.assertEqual((3, 2), thumb.size)
