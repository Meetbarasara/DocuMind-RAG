from setuptools import setup, find_packages
from typing import List 

def get_requirements(file_path: str) -> List[str]:
  """
  This function will return the list of requirements
  """
  with open(file_path) as file_obj:
    requirements = file_obj.readlines()
    
    #Remove newline characters 
    requirements = [req.replace("\n", "") for req in requirements]

    if '-e .'  in requirements:
      requirements.remove('-e .')

    
    return requirements

setup(
    name='Documind',
    version='0.1.0',
    packages=find_packages(),
    install_requires=get_requirements('requirements.txt'),
    description='RAG based document question answering system',
    author='Meet',
)