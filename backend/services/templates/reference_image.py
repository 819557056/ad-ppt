"""Bounded visual-only input for the text model's reference-image path."""
import base64
from io import BytesIO

from PIL import Image, ImageOps

from services.scene.versioning import SceneError


def reference_data_url(payload):
    """Normalize a private preview into a modest, model-compatible JPEG."""
    try:
        with Image.open(BytesIO(payload)) as source:
            if getattr(source, 'n_frames', 1) != 1 or source.width * source.height > 40_000_000:
                raise ValueError('reference image dimensions unsupported')
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            if image.mode in ('RGBA', 'LA') or 'transparency' in image.info:
                rgba = image.convert('RGBA')
                opaque = Image.new('RGB', rgba.size, '#FFFFFF')
                opaque.paste(rgba, mask=rgba.getchannel('A'))
            else:
                opaque = image.convert('RGB')
            output = BytesIO()
            opaque.save(output, format='JPEG', quality=85, optimize=True)
            encoded = output.getvalue()
    except (OSError, ValueError) as exc:
        raise SceneError('REFERENCE_IMAGE_INVALID', 'Reference preview cannot be decoded', 422) from exc
    if len(encoded) > 4 * 1024 * 1024:
        raise SceneError('REFERENCE_IMAGE_TOO_LARGE', 'Reference preview exceeds model input limit', 422)
    return 'data:image/jpeg;base64,' + base64.b64encode(encoded).decode('ascii')
