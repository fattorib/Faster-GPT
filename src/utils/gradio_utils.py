"""
Helper descriptions for gradio app
"""

description_star = """GPT-* is a pair of transformer models based on GPT2-Small and GPT2-Medium with the following architecture modifications to speed up training and inference:

1. Parallel residual connections (as introduced in [GPT-J](https://github.com/kingoflolz/mesh-transformer-jax))
2. Increasing the dimension of each attention head from 64 to 128 (as introduced in [GPT-J](https://github.com/kingoflolz/mesh-transformer-jax)) 
3. [ALiBi](https://arxiv.org/abs/2108.12409) position embeddings (training at context length of 512 tokens)

### Dataset 📚: 
GPT-* was training for one epoch (roughly ~26B tokens) on a dataset consisting of [OpenWebText1](https://github.com/jcpeterson/openwebtext), [OpenWebText2](https://arxiv.org/abs/2101.00027), [BookCorpus2](https://arxiv.org/abs/2101.00027), [Books3](https://arxiv.org/abs/2101.00027)
and [PhilPapers](https://arxiv.org/abs/2101.00027).

### Text Generation 📜:
The following text generation methods are supported:
* Top-K Sampling
* Nucleus Sampling 
* Typical Sampling
"""

description_354 = """GPT-354M is a transformer model based on GPT2-Medium. While it was originally trained as a small benchmark model for the GPT-* models, it is reasonably good at producing coherent text!

### Dataset 📚: 
GPT-354M was training for one epoch (roughly ~21B tokens) on a dataset consisting of [OpenWebText1](https://github.com/jcpeterson/openwebtext),[BookCorpus2](https://arxiv.org/abs/2101.00027), and [Books3](https://arxiv.org/abs/2101.00027).

### Text Generation 📜:
The following text generation methods are supported:
* Top-K Sampling
* Nucleus Sampling 
* Typical Sampling
"""

DESCRIPTION_MAP = {
    "base*": description_star,
    "medium*": description_star,
    "medium": description_354,
}
