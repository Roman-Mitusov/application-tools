import io
import os
import logging
import fitz  # PyMuPDF
from PIL import Image
import numpy as np
from typing import Optional, Any, Dict, List


from pydantic import BaseModel, Field, model_validator, PrivateAttr
from langchain_core.tools import ToolException

from ..elitea_base import BaseToolApiWrapper
from ..utils import create_pydantic_model

logger = logging.getLogger(__name__)

MIME_TO_EXTENSION = {
    "image/jpeg": [".jpg", ".jpeg"],
    "image/png": [".png"],
    "image/gif": [".gif"],
    "image/bmp": [".bmp"],
    "image/tiff": [".tiff", ".tif"],
    "application/pdf": [".pdf"]
}


PAGE_DESCRIPTION_PROMPT_TEMPLATE = """
Please create detailed description of provided image.
Ignore page header, footer, basic logo and background.
Describe all images (illustration), tables.
Text with bullet points is NOT a table or image.

Use only provided information.
DO NOT make up answer.

Provide answer in JSON format with fields:
{{
    "page_summary": "page summary here",
    "keyfact"     : "the most important fact from the image",
    "image_quality": {{
        "level": "level of image quality (normal, detailed)", 
        "explanation": "explain why this detailisation is required"
    }},
    "images":[
        {{
            "description": "image description",
            "type"       : "image type (photo, illustration, diagram, etc.)",
            "keyfact"    : "the most important fact from the image"
        }}
    ],
    "tables":[
        {{
            "description": "table description",
            "keyfact"    : "the most important fact from the table"
        }}
    ]
}}
"""

IMAGE_OCR_PROMPT_TEMPLATE = """Please extract all text from the image."""


# Models for tool arguments
class RecognizeArgs(BaseModel):
    prompt: Optional[str] = Field(None, description="Optional prompt to guide OCR extraction")

class ExtractPDFArgs(BaseModel):
    prompt: Optional[str] = Field(None, description="Optional prompt to guide PDF extraction")

class BatchProcessImagesArgs(BaseModel):
    prompt: Optional[str] = Field(None, description="Optional prompt to process the batch of images")
    detect_images: bool = Field(False, description="Whether to detect and crop images within pages before processing")

class OCRApiWrapper(BaseToolApiWrapper):
    """Wrapper for OCR tools that can use either LLM-based vision capabilities or tesseract"""
    
    llm: Any = None
    struct_llm: Any = None
    alita: Any = None
    artifacts_folder: str = ""
    tesseract_settings: Dict[str, Any] = {}
    structured_output: bool = False
    expected_fields: Dict[str, Any] = {}
    
    
    @model_validator(mode='after')
    def validate_settings(self):
        """Validate that either LLM or tesseract is properly configured"""
        # self.artifact = self.alita.artifact(self.artifacts_folder)
        if self.structured_output and len(self.expected_fields.keys()) > 0:
            stuct_model = create_pydantic_model(f"OCR_{self.artifacts_folder}_Output", self.expected_fields)
            self.struct_llm = self.llm.with_structured_output(stuct_model)
        else:
            self.struct_llm = self.llm
        
        return self
    
    def _process_with_llm(self, image_path: str, prompt: Optional[str] = None) -> str:
        """Process image with LLM-based vision capabilities"""
        if not self.struct_llm:
            raise ToolException("LLM not configured for OCR processing")
        try:
            # Read image as base64
            import base64
            
            # Determine the correct MIME type based on file extension
            file_extension = os.path.splitext(image_path.lower())[1]
            mime_type = "image/jpeg"  # Default
            
            # Find the correct MIME type for this extension
            for mime, extensions in MIME_TO_EXTENSION.items():
                if file_extension in extensions:
                    mime_type = mime
                    break
            
            # Get the data directly without using context manager
            data = self.alita.download_artifact(self.artifacts_folder, image_path)
            base64_image = base64.b64encode(data).decode('utf-8')
                
            if not prompt:
                prompt = IMAGE_OCR_PROMPT_TEMPLATE
            
            messages = [
                {"role": "user", 
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", 
                    "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}
                ]}
            ]
            logger.info(f"LLM messages: {messages}")
            response = self.struct_llm.invoke(messages)
            if self.structured_output:
                return response.model_dump()
            return response.contents
            
        except Exception as e:
            raise ToolException(f"Error processing image with LLM: {e}")
    
    def _process_with_tesseract(self, image_path: str) -> str:
        """Process image with Tesseract OCR"""
        try:
            import pytesseract
            from PIL import Image
            
            # Get data directly without context manager
            data = self.alita.download_artifact(self.artifacts_folder, image_path)
            image = Image.open(io.BytesIO(data))
            
            # Apply any tesseract settings
            config = self.tesseract_settings.get('config', '')
            lang = self.tesseract_settings.get('lang', 'eng')
            return pytesseract.image_to_string(image, lang=lang, config=config)
        except Exception as e:
            raise ToolException(f"Error processing image with Tesseract: {e}")
    
    def recognize(self, prompt: Optional[str] = None) -> str:
        """
        Recognize text in images from the artifacts folder.
        Uses LLM if configured, otherwise falls back to Tesseract OCR.
        """
        # Find images in artifacts folder
        image_files = []
        supported_extensions = [ext for exts in MIME_TO_EXTENSION.values() for ext in exts]
        
        for f in self.alita.list_artifacts(self.artifacts_folder).get('rows', []):
            file_extension = os.path.splitext(f['name'].lower())[1]
            if file_extension in supported_extensions:
                image_files.append(f['name'])
        
        if not image_files:
            raise ToolException(f"No image files found in {self.artifacts_folder}")
        
        results = []
        for img_path in image_files:
            if self.tesseract_settings:
                result = self._process_with_tesseract(img_path)
            else:
                result = self._process_with_llm(img_path, prompt)
            results.append({
                "filename": img_path,
                "text": result
            })
        return results
    
    def _pdf_to_images(self, pdf_path: str) -> List[str]:
        """Convert PDF pages to images and store them in artifacts folder, removing white borders and skipping blank pages"""
        try:
            # Get PDF data
            pdf_data = self.alita.download_artifact(self.artifacts_folder, pdf_path)
            
            # Open PDF from memory
            doc = fitz.open(stream=pdf_data, filetype="pdf")
            image_paths = []
            
            # Create base filename without extension
            base_filename = os.path.splitext(pdf_path)[0]
            
            # Convert each page to an image
            for page_num, page in enumerate(doc):
                # Render page to an image with higher resolution
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                
                # Convert to PIL Image
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                
                # Convert to numpy array
                img_array = np.array(img)
                
                # Detect if page is blank by checking pixel values
                # Calculate the average brightness of the image
                gray = np.mean(img_array, axis=2)
                avg_brightness = np.mean(gray)
                
                # Count non-white pixels (pixels below threshold)
                threshold = 245  # Threshold for considering a pixel as non-white
                non_white_pixels = np.sum(gray < threshold)
                non_white_ratio = non_white_pixels / (img_array.shape[0] * img_array.shape[1])
                
                # Skip if page is mostly blank (high brightness and few non-white pixels)
                if avg_brightness > 250 and non_white_ratio < 0.01:
                    logger.info(f"Skipping blank page {page_num + 1} in {pdf_path}")
                    continue
                
                # Detect the background color (usually white)
                bg_color = img_array[0, 0, :]  # Top-left pixel color
                
                # Find non-white pixels for cropping
                non_white = np.where(gray < 250)  # Threshold for white
                
                # If there are non-white pixels, crop
                if len(non_white[0]) > 0:
                    min_row, max_row = np.min(non_white[0]), np.max(non_white[0])
                    min_col, max_col = np.min(non_white[1]), np.max(non_white[1])
                    
                    # Add some padding
                    padding = 10
                    min_row = max(0, min_row - padding)
                    min_col = max(0, min_col - padding)
                    max_row = min(img_array.shape[0], max_row + padding)
                    max_col = min(img_array.shape[1], max_col + padding)
                    
                    # Crop the image
                    img = img.crop((min_col, min_row, max_col, max_row))
                
                # Convert to bytes
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='PNG')
                img_bytes = img_byte_arr.getvalue()
                
                # Create a unique filename for the page image
                image_filename = f"{base_filename}_page_{page_num + 1}.png"
                
                # Upload to artifacts folder
                self.alita.create_artifact(self.artifacts_folder, image_filename, img_bytes)
                image_paths.append(image_filename)
                
            return image_paths
            
        except Exception as e:
            raise ToolException(f"Error converting PDF to images: {e}")
    
    
    def process_pdf(self, prompt: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Process PDF files in the artifacts folder.
        Converts each page to an image, removes white borders, and processes all images in a batch.
        Returns the results of the processing. Temporary images are deleted after processing.
        """
        pdf_files = []
        
        for f in self.alita.list_artifacts(self.artifacts_folder).get('rows', []):
            file_extension = os.path.splitext(f['name'].lower())[1]
            if file_extension == ".pdf":
                pdf_files.append(f['name'])
        
        if not pdf_files:
            raise ToolException(f"No PDF files found in {self.artifacts_folder}")
        
        results = []
        for pdf_path in pdf_files:
            # Convert PDF to images
            page_images = self._pdf_to_images(pdf_path)
            
            if not page_images:
                continue
                
            # Process all images in a batch with a single message
            if self.llm:
                # Create a message with all images
                import base64
                
                # Set default prompt if not provided
                if not prompt:
                    prompt = IMAGE_OCR_PROMPT_TEMPLATE
                
                # Prepare content array with text prompt and all images
                content = [{"type": "text", "text": prompt}]
                
                # Add all images to the message
                for img_path in page_images:
                    # Download the image
                    image_data = self.alita.download_artifact(self.artifacts_folder, img_path)
                    
                    # Determine MIME type based on file extension
                    file_extension = os.path.splitext(img_path.lower())[1]
                    mime_type = "image/png"  # Default for PNG
                    
                    # Find the correct MIME type for this extension
                    for mime, extensions in MIME_TO_EXTENSION.items():
                        if file_extension in extensions:
                            mime_type = mime
                            break
                    
                    # Encode image to base64
                    base64_image = base64.b64encode(image_data).decode('utf-8')
                    
                    # Add to content
                    content.append({
                        "type": "image_url", 
                        "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}
                    })
                
                # Create the messages array with user role
                messages = [{"role": "user", "content": content}]
                
                # Process with LLM
                logger.info(f"Processing batch of {len(page_images)} images for PDF {pdf_path}")
                response = self.struct_llm.invoke(messages)
                
                if self.structured_output:
                    text_result = response.model_dump()
                else:
                    text_result = response.content
                
                # Add to results
                results.append({
                    "pdf_filename": pdf_path,
                    "page_images": page_images,
                    "total_pages": len(page_images),
                    "extracted_text": text_result
                })
            else:
                # Fall back to tesseract for each image individually
                text_results = []
                for img_path in page_images:
                    text_result = self._process_with_tesseract(img_path)
                    text_results.append({
                        "page": img_path,
                        "text": text_result
                    })
                
                # Add to results
                results.append({
                    "pdf_filename": pdf_path,
                    "page_images": page_images,
                    "total_pages": len(page_images),
                    "extracted_text": text_results
                })
            
            # Clean up the temporary image files after processing
            logger.info(f"Cleaning up {len(page_images)} temporary image files")
            for img_path in page_images:
                try:
                    self.alita.delete_artifact(self.artifacts_folder, img_path)
                    logger.debug(f"Deleted temporary image: {img_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete temporary image {img_path}: {e}")
        
        return results

    def get_available_tools(self):
        """Get available OCR tools"""
        return [
            {
                "name": "recognize",
                "description": "Recognize text in images from the artifacts folder",
                "args_schema": RecognizeArgs,
                "ref": self.recognize
            },
            {
                "name": "process_pdf",
                "description": "Convert PDF pages to images and store in artifacts folder",
                "args_schema": ExtractPDFArgs,
                "ref": self.process_pdf
            }
        ]