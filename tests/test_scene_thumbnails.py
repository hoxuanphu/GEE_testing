import io
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from app import aws_stac, cdse


class SceneThumbnails(unittest.TestCase):
    def test_aws_normalization_keeps_only_a_public_preview_path(self):
        trusted = ('https://sentinel-cogs.s3.us-west-2.amazonaws.com/'
                   'sentinel-s2-l2a-cogs/48/P/ZC/2026/11/scene/preview.jpg')
        def feature(href):
            return {'id': 'scene', 'assets': {'thumbnail': {'href': href}}}

        normalize = aws_stac._normalize_features
        self.assertEqual(normalize([feature(trusted)])[0]['thumbnail_url'], trusted)
        self.assertNotIn('thumbnail_url', normalize([feature(trusted + '?signature=x')])[0])
        self.assertNotIn('thumbnail_url', normalize([feature(trusted.replace('amazonaws.com', 'example.com'))])[0])

    def test_cdse_only_reads_quicklook_asset_from_the_selected_product(self):
        product = '0a9798ab-9366-4fa3-80e7-eaae981d7dcf'
        asset = 'b223ef71-0e81-416c-af0c-cdeb23d754e8'
        metadata = Mock()
        metadata.__enter__ = Mock(return_value=metadata)
        metadata.__exit__ = Mock(return_value=False)
        metadata.json.return_value = {'Assets': [
            {'Type': 'OTHER', 'Id': product},
            {'Type': 'QUICKLOOK', 'Id': asset, 'DownloadLink': 'https://example.com/wrong'},
        ]}
        image = Mock()
        image.__enter__ = Mock(return_value=image)
        image.__exit__ = Mock(return_value=False)
        raw = io.BytesIO()
        Image.new('RGB', (384, 192), (12, 34, 56)).save(raw, format='JPEG')
        image.iter_content.return_value = [raw.getvalue()]
        with patch.object(cdse, '_ensure_env'), patch.object(cdse.requests, 'get', side_effect=[metadata, image]) as get:
            media_type, content = cdse.scene_thumbnail(product)
        self.assertEqual(media_type, 'image/png')
        with Image.open(io.BytesIO(content)) as thumbnail:
            self.assertEqual(thumbnail.size, (192, 96))
        self.assertTrue(get.call_args_list[0].args[0].endswith(f'/Products({product})'))
        self.assertTrue(get.call_args_list[1].args[0].endswith(f'/Assets({asset})/$value'))
