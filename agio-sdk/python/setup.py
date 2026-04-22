from setuptools import setup, find_packages

setup(
    name="agio-sdk",
    version="0.0.1",
    description="AGIO SDK — Cross-chain micropayments for AI agents",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="AGIO Protocol",
    author_email="hello@agiotage.finance",
    url="https://github.com/agio-protocol/agio-sdk",
    packages=find_packages(),
    python_requires=">=3.9",
    classifiers=[
        "Development Status :: 1 - Planning",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development :: Libraries",
    ],
)
