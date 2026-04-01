import os
import sys 
import json
import logging
from typing import List ,any, Dict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent.parent.parent))

from unstructured.partition.csv import partition_csv
from unstructured.partition.docx import partition_docx
from unstructured.partition.email import partition_email
from unstructured.partition.html import partition_html
from unstructured.partition.json import partition_json
from unstructured.partition.pdf import partition_pdf
from unstructured.partition.pptx import partition_pptx
from unstructured.partition.text import partition_text
from unstructured.partition.xml import partition_xml
from unstructured.partition.xlsx import partition_xlsx
from unstructured.chunking.title import chunk_by_title

from langchain_core.documents import Document

try :
    from .config import Config
except ImportError:
    from src.components.config import Config


class DocumentProcessor:
    def __init__(self, config):
        self.config = config

    def process_documents(self, file_paths: str) -> List:
        """Process documents and return a list of processed data."""
        file_extension = Path(file_paths).suffix.lower()
        file_name = Path(file_paths).name

        try:
            # --- Existing File Types ---
            if file_extension == ".pdf":
                elements = partition_pdf(
                    filename=file_paths,                      # pdf path
                    strategy='hi_res',                        # most accurate strategy
                    infer_table_structure=True,               # table in html format
                    extract_image_block_types=["Image"],      # image grab
                    extract_image_block_to_payload=True,      # store image as base64 in payload 
                )

            elif file_extension == ".docx":
                elements = partition_docx(
                    filename=file_paths,
                    infer_table_structure=True,
                )   

            elif file_extension == ".pptx":
                elements = partition_pptx(filename=file_paths)     
            
            elif file_extension == ".xlsx":
                elements = partition_xlsx(filename=file_paths)
            
            elif file_extension in [".txt", ".md"]:
                elements = partition_text(filename=file_paths)
            
            # --- Newly Added File Types ---
            elif file_extension == ".csv":
                elements = partition_csv(filename=file_paths)
                
            elif file_extension in [".html", ".htm"]:
                elements = partition_html(filename=file_paths)
                
            elif file_extension == ".json":
                elements = partition_json(filename=file_paths)
                
            elif file_extension == ".xml":
                elements = partition_xml(filename=file_paths)
                
            elif file_extension in [".eml", ".msg"]:
                elements = partition_email(filename=file_paths)

            else:
                raise ValueError(f"Unsupported file type: {file_extension}")
            
            print(f"Processed {file_paths} successfully. Extracted {len(elements)} elements.\n")

            # Analyzed elements 
            # Note: Ensure _log_elements_analysis is defined in your class or imported
            self._log_elements_analysis(elements) 

            # source metadata
            for element in elements:  
                # Some elements might not have a metadata object initially, 
                # but unstructured usually handles this.
                if hasattr(element, 'metadata'):
                    element.metadata.filename = file_name
                    element.metadata.filetype = file_extension.strip(".")
                    element.metadata.filepath = file_paths
                
            return elements
        
        except Exception as e:
            print(f"Error processing {file_paths}: {e}")
            return []