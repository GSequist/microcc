from micro_cc.utils.helpers import sanitize_and_encode_image_
from micro_cc.models import model_call
import os


async def vision(img_path: str, query: str, *, project_dir, model) -> str:
    """Analyze an image and answer a specific question about it — an
    isolated, context-free call. Used internally by computer_tool_/
    browser_tool_ to analyze a screenshot they just took as one piece of a
    larger composite text result; NOT registered as a model-facing tool
    (the model reads image files natively via read_, see file_tools_.py —
    Read is multimodal by extension, with no separate vision/image tool).

    Args:
        img_path: Absolute or relative path to the image file
        query: What to ask about the image
    """
    try:
        if not os.path.isabs(img_path):
            img_path = os.path.join(project_dir, img_path)

        if not os.path.exists(img_path):
            return f"[File not found: {img_path}] — (potential issue: macOS uses special Unicode characters in filenames when screenshot is taken - ask user to try to rename the file for you!)"

        encoded_string = sanitize_and_encode_image_(img_path)
        resp = await model_call(
            input=query, encoded_image=encoded_string, model=model
        )
        # content[0] isn't reliably the answer — reasoning models (Ollama's
        # "thinking"-tagged tags default to reasoning even without an
        # explicit thinking=True) prepend a thinking block, whose .text is
        # always "", pushing the real answer to a later index.
        text_blocks = [b.text for b in resp.content if b.type == "text"]
        return "\n".join(text_blocks) if text_blocks else "[Vision: model returned no text content]"

    except Exception as e:
        return f"Vision error: {str(e)}"
