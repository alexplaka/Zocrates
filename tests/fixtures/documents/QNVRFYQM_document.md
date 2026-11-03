---
type: preprint
okf_version: '0.2'
item_key: QNVRFYQM
parent_item_key: X88DZ9HX
title: 'Prot2Chat: Protein LLM with Early-Fusion of Text, Sequence and Structure'
member_of_collections: []
source:
  path: /Zotero/Zotero test-user/storage/QNVRFYQM/Wang et al. - 2025 - Prot2Chat
    Protein LLM with Early-Fusion of Text, Sequence and Structure.pdf
  hash: 4cb4d6d3db604e71e1dcdd6e6d8daeb6979290369cd07d4ecd416be07d44164f
  content_type: application/pdf
provenance:
  converter_id: datalab
  converter_version: 0.5.0
  converted_at: '2026-08-19T21:12:29.358554+00:00'
  checkpoint_id: checkpoint_m0vJLJiqSUf36n7hmIBCpQQANo-2OmKGZ32Sbpe18CY
---

## PAPER

# Prot2Chat: Protein LLM with Early-Fusion of Text, Sequence and Structure

Zhicong Wang,<sup>1</sup> Zicheng Ma,<sup>2</sup> Ziqiang Cao,<sup>1,*</sup> Changlong Zhou,<sup>1</sup> Jun Zhang<sup>2</sup>and Yiqin Gao<sup>2</sup>

<sup>1</sup>School of Computer Science and Technology, Soochow University, 215006, Suzhou, China and <sup>2</sup>Changping Laboratory, Peking University, 102206, Beijing, China

*Corresponding author. zqcao@suda.edu.cn

FOR PUBLISHER ONLY Received on Date Month Year; revised on Date Month Year; accepted on Date Month Year

## Abstract

**Motivation:** Proteins are of great significance in living organisms. However, understanding their functions encounters numerous challenges, such as insufficient integration of multimodal information, a large number of training parameters, limited flexibility of classification-based methods, and the lack of systematic evaluation metrics for protein Q&A systems. To tackle these issues, we propose the Prot2Chat framework.

**Results:** We modified ProteinMPNN to encode protein sequence and structural information in a unified way. We used a large language model (LLM) to encode questions into vectors and developed a protein-text adapter to compress protein information into virtual tokens based on these vectors, achieving the early fusion of text and protein information. Finally, the same LLM reads the virtual tokens and the questions to generate answers. To optimize training efficiency, we froze the encoder and employed Low-Rank Adaptation (LoRA) techniques for the LLM. Experiments on two datasets show that both automated metrics and expert evaluations demonstrate the superior performance of our model, and zero-shot prediction results highlight its generalization ability. The models and codes are available at [https://github.com/wangzc1233/Prot2Chat](https://github.com/wangzc1233/Prot2Chat).

**Contact:** zqcao@suda.edu.cn or wangzc025@163.com

**Key words:** Protein Q&A, Early-Fusion, LLM

## Introduction

With the continued advancement of biotechnology, gaining a deeper understanding of protein sequences, structures, and their functions has become increasingly critical. Protein function is fundamentally governed by its amino acid sequence, which encodes the structural and biochemical features necessary for activity. However, even identical protein sequences can adopt distinct three-dimensional conformations under varying environmental conditions or subtle mutations, thereby exhibiting diverse biological functions, as exemplified by cases reported in AFcluster [30]. The widespread availability of protein sequence and structure data—enabled by high-throughput sequencing technologies and powerful structure prediction tools [2] has significantly advanced our understanding of the physicochemical properties of proteins. In parallel, significant research efforts have focused on investigating and predicting protein functions, where deep learning models have employed sequence or structure representations as input to predict function-related properties such as Gene Ontology (GO) terms [11, 23]. The recent emergence of large language models (LLMs) has propelled protein question answering (Q&A) systems at the forefront of

computational biology research [33]. The related systems enable researchers to efficiently access and interpret key information regarding protein properties and functions. This holds great potential for transformative applications in fields such as drug discovery and disease research [22]. To support this growing field, several datasets, such as Mol-Instructions [8] and UniProtQA [16], have been introduced, providing essential resources for advancing protein Q&A systems. An increasing number of models have fine-tuned LLMs on protein-specific Q&A datasets to generate contextually relevant text or to answer questions related to protein structure and function [34].

However, as summarized in Table 1, existing methods face several critical limitations, including inadequate integration of multimodal information, restricted flexibility, high training parameter volume, and a lack of systematic evaluation metrics. These challenges constrain the models' ability to generate accurate and context-aware responses. For instance, although models such as FAPM [31] and InstructProtein [29] incorporate generative Q&A capabilities, they remain heavily dependent on classification-based paradigms. This often leads to the production of unexpected content, deviating from the intended scope of inquiry and reducing the models' effectiveness in

<figure data-type="image">

![Fig. 1](images/9ba3dc91984c80b96f217fb1bddd5c06_img.jpg)

<synthetic_caption>
Diagram illustrating the Prot2Chat framework. A dashed box contains two speech bubbles. The top blue bubble, associated with a human icon, contains a soft prompt: ' Assess the function of the protein and its possible involvement in a biological process, based on the protein structure and the amino acid sequence.' The bottom green bubble, associated with a robot icon, contains the model's response: 'The protein structure and sequence under consideration appears to have a primary function of DNA binding, tRNA binding, and it may be associated with the DNA transposase activity.'
</synthetic_caption>

**Fig. 1.** Prot2Chat can assist human in understanding protein information and achieve cross-modal information communication. Among them, ‘<Soft Prompt>’ is the prompt obtained by our model through fusing protein structure, sequence, and text information, which helps the LLM generate more valuable answers.

</figure>

handling complex protein-related questions. BiomedGPT [16] enables users to interact with biological data through natural language. Nonetheless, it relies exclusively on sequence-level information, which substantially limits its predictive capacity. ProtChatGPT [28] and Evola [34] attempt to address this limitation by employing separate modules for encoding sequence and structural information. However, this modular design hinders effective interaction between sequence and structural features and significantly increases computational complexity. Moreover, no existing model has systematically examined the relative contributions of sequence and structural information to the performance of protein Q&A systems.

As the sequence and structural features of proteins are in inseparable correspondence, both jointly determine the properties of proteins. We propose a novel framework named Prot2Chat. This framework can seamlessly integrate the spatial structure and sequence information of proteins into LLMs. Meanwhile, by incorporating text information during the processing of protein information, the LLM is enabled to understand the question, so as to achieve question-based compression of protein information, and ultimately realize the early fusion of text, structure, and sequence. This approach seeks to enhance both the performance and applicability of protein Q&A systems. To be specific, we introduce protein sequence information during the initialization of nodes features in ProteinMPNN [4] and early-fuse the protein sequence information in the protein feature representation. Then inspired by BLIP-2 [12], an adapter module was incorporated to align the protein encoder with a LoRA [10] fine-tuned LLM. In addition, we use the LLM to encode the question text and add it to the adapter, integrating it into the above-mentioned compression process to further achieve the early-fusion of protein and text information, aiming to improve the accuracy and robustness of protein Q&A. The processed protein and text data are provided as a soft prompt to the LLM. Leveraging the flexible text generation capabilities of the LLM, our model effectively follows residue-level protein instructions,

significantly enhancing its utility in protein-related tasks. As shown in Figure 1, the answering effect of our model can provide effective insights for human to explore proteins in a Q&A manner familiar to them.

To evaluate the model’s performance, we conducted comparative experiments on the Mol-Instructions [8] and UniProtQA [16] datasets, and performed zero-shot experiments on a subset of UniProtQA to validate generalization. We used common metrics like BLEU and ROUGE, as well as online KIMI [20] scoring metrics and expert evaluations, ensuring the reliability and relevance of the answers. Experimental results show that Prot2Chat which integrates protein sequences and structures using a unified encoder and fuses text information at an early stage outperforms those using sequence-only pretraining, ESM-based encoders [14], and multi-encoders. Besides, its effectiveness far outperforms that of late-fusion, which means that text information is only utilized when the LLM generates answers. These findings highlight the effectiveness of early-fusion strategies for integrating multimodal protein representations. Furthermore, the high consistency between expert evaluations and online KIMI assessments confirms the robustness of Prot2Chat.

In summary, our contributions are as follows:

1. We extended the existing structure encoder ProteinMPNN to realize structure and sequence early-fusion without the need for training.
2. Based on this protein encoder, we designed a text-aware protein-text adapter module. By re-utilizing the LLM, we achieved question-based compression of protein information, early-fusion of text and protein information, and implemented a lightweight and efficient protein LLM with only 109 million training parameters.
3. We performed various systematic assessments across various evaluation datasets to validate our model’s generative and generalization capabilities. These included traditional metrics, large model evaluations online, and manual expert evaluations.

### Related work

### Protein Representation Learning

Proteins are fundamental components of cells, essential for their biological activities and diverse functions. Previous studies on protein characterization have explored various methods to learn protein representations based on different forms of protein information. Protein sequences, often referred to as the “language of life,” have been extensively studied using advanced natural language processing techniques. For instance, Tranception [18] employs the Transformer [27] model to encode amino acid sequences, capturing relationships between residues and autoregressively reconstructing protein sequences from large-scale databases. Similarly, sequential modeling approaches such as ESM [15] leverage masked language modeling (MLM) to develop attention patterns that correspond to residue-residue contact maps, enhancing sequence-based protein representations. On the other hand, structure-based approaches [9] directly indicate protein function and encode geometric information of the protein for topology-sensitive tasks such as protein property prediction. Foldseek [26] introduces the concept of a structural alphabet, encoding protein structures into a discrete representation space. Similarly, Saprot [23] introduces a structure-aware vocabulary,

<figure data-type="table">

**Table 1.** Comparison of input-output of protein Q&A models, "Description" is a text related to protein functions, such as the description of Gene Ontology (GO) terms, "[Structure+Sequence+Question]" represents the early-fusion of structure, sequence and text.

| Model | Input | Output |
| --- | --- | --- |
| Prot2Text | Sequence | Structured Text |
| ProtNote | Sequence&Description | Function&Probability |
| ProtChatGPT | Structure&Sequence&Question | Free Text |
| Evola | Structure&Sequence&Question | Free Text |
| InstructProtein | Sequence&Question | Structured Text |
| FAPM | Sequence | Structured Text |
| BioMedGPT | Sequence&Question | Free Text |
| Prot2Chat | [Structure+Sequence+Question]&Question | Free Text |

</figure>

embedding structural information into model input to enhance representational capacity, achieving significant success in protein function prediction tasks.

### Multimodal Alignment

Enabling LLMs to understand extra modalities has been a fast-evolving research area, with notable examples including image-text models [12], video-text models like VideoLlama [32], audio-text models such as Macaw-LLM [17], and molecular-text models like MolTC [7]. This line of research was pioneered by advancements in visual language modeling (VLM), to enable LLMs to understand images, leading VLM methods adopt different strategies. Some, like BLIP-2 [12], employ nonlinear and expressive cross-modal projectors, while others, like PaLM-E [5], utilize visual encoders and fine-tune LLMs on multimodal datasets. These approaches have also been increasingly applied to protein analysis. For instance, Galactica [24] leverages scientific literature to model protein sequences and SMILES representations, enabling the model to interpret sequence properties. Some of these methods attempt to adopt a Q&A approach that is closer to human habits to explore and understand the functions of proteins. ProtNote [3], utilize two encoders to encode protein sequence information and text information, respectively, and Multilayer Perceptron (MLP) to fuse these inputs, achieving the goal of using text to achieve supervised and zero-shot protein functional prediction. ProtChatGPT [28] uses two independent modules to encode sequence and structural information respectively, and aligns them with natural language to explore protein functions. InstructProtein [29] employs a supervised training framework based on instruction generation using knowledge graphs, enabling bidirectional generation between natural language and protein language. This model can predict protein functional descriptions and generate protein sequences based on natural language prompts. Prot2Text [1] combines graph neural networks (GNNs) with LLMs to predict protein functions in free-text form. FAPM [31] utilizes a contrastive learning framework to implement a generative-like Q&A model, while BiomedGPT [16] bridges the gap between biological and human natural language through a multimodal generative pre-trained transformer (GPT). This allows users to "communicate" with biological modalities using free-text queries, facilitating interactions with biological data in natural language.

<figure data-type="table">

**Table 2.** The count and division of the datasets we used.

| Dataset | Train | Valid | Test |
| --- | --- | --- | --- |
| Mol-Instructions | 404640 | 16859 | 11072 |
| UniProtQA | 25820 | 1075 | 6734 |

</figure>

## Method

### Overview of Prot2Chat

As illustrated in Figure 2, the overall architecture of our model comprises three components: the protein encoder, the text-aware protein-text adapter, and the LLM. Our model leverages the 3D coordinate structure information of protein residue atoms along with the protein sequence and natural language question text. The protein encoder processes protein information to generate a protein embedding representation, which is then transformed into a text-aligned protein prompt via the text-aware protein-text adapter, with question text information being fused simultaneously. This prompt, combined with the input question, serves as the input to the large language model, ultimately producing the output answer.

### Sequence and Structure Fused Protein Encoder

We modified the structure encoder ProteinMPNN to realize the sequence and structure early-fusion. ProteinMPNN is originally dedicated to design protein sequences based on backbone structures. The input is structural information  $E$ , namely the 3D coordinates of protein residue atoms ( $N$ : amino nitrogen,  $C\alpha$ : alpha carbon,  $C$ : carbonyl carbon and  $O$ : carbonyl oxygen). Then protein features  $h_V$  are obtained through the encoder layer and the decoder layer.

$$h_E = \text{Linear}(E) \quad (1)$$

$$h_V = \text{Zeros}(n, D_c) \quad (2)$$

$$h_V, h_E = \text{Encoderlayers}(h_V, h_E) \quad (3)$$

$$h_V = \text{Decoderlayers}(h_V, h_E) \quad (4)$$

where Linear, Encoderlayers and Decoderlayers are all modules in ProteinMPNN,  $E$  is the structural features and  $n$  is the number of protein residues and  $D_c$  is the embedding dimension of the protein encoder. We find that a variant version of ProteinMPNN additionally contains the Embedding module for protein sequences  $S$ , and the Decoderlayer is sequence-aware, which changes Equation 4 to:

$$h_V = \text{Decoderlayers}(h_V, h_E, \text{Embedding}(S)) \quad (5)$$

<figure data-type="image">

![Fig. 2](images/2fa4a1bf91d0f34e87c689fbc1211fe3_img.jpg)

<synthetic_caption>
The diagram illustrates the architecture of Prot2Chat. It shows the flow from input to output. Inputs include 'Protein Structure' (represented by a 3D protein model) and 'Protein Sequence' (a string of amino acids like 'MTQWKRRS RRKGMTR...'). These are fed into a 'Protein Encoder' (marked with a snowflake icon, indicating it is frozen). The encoder produces a 'Protein Embedding' (a grid of green blocks). Simultaneously, 'Question Text' (a document icon) is processed by a 'Text-Aware Adapter' (marked with a flame icon, indicating it is trainable) to produce a 'Question Vector' (an orange bar). The 'Question Vector' and the 'Protein Embedding' are combined in a 'Soft Prompt' (a grid of green and yellow blocks). This 'Soft Prompt' is then fed into a 'LoRA LLM' (marked with a flame icon). The final output is the 'Answer' (a document icon with a checkmark), which is generated by 'Decoding' the 'LoRA LLM' output.
</synthetic_caption>

**Fig. 2.** Model Structure of Prot2Chat. The red font represents the input, the snowflake represents freezing, and the flame represents the parameters to be trained. We obtain the embedding with multi-dimensional feature fusion of proteins from protein structure and sequence information through an encoder. Meanwhile, we get the question vector from the question text. Then, we conduct early-fusion and alignment of this vector with protein information to obtain the soft prompt. Finally, we input the soft prompt along with the question text into the LLM to get the answer.

</figure>

<figure data-type="table">

**Table 3.** Result in Mol-instructions protein oriented dataset, the best performances are marked in bold.

| Model | BLEU-2 | ROUGE-1 | ROUGE-2 | ROUGE-L |
| --- | --- | --- | --- | --- |
| LLaMA3-8B-Instruct | 4.84 | 23.07 | 4.31 | 15.36 |
| LLaMA3-FT with sequence | 6.42 | 24.50 | 6.32 | 17.03 |
| BioMedGPT-LM-10B | 1.02 | 10.93 | 1.57 | 7.84 |
| Evola-10B | 8.69 | 29.09 | 8.41 | 20.04 |
| KIMI (Zero-shot) | 4.79 | 22.21 | 4.62 | 14.70 |
| KIMI (Few-shot) | 12.05 | 31.21 | 11.38 | 24.18 |
| Prot2Chat | **35.85** | **57.21** | **38.09** | **50.51** |

</figure>

<figure data-type="table">

**Table 4.** Results of the ablation experiments in Mol-instructions dataset. The best performance is marked in bold.

| Model | BLEU-2 | ROUGE-1 | ROUGE-2 | ROUGE-L |
| --- | --- | --- | --- | --- |
| Prot2Chat | **35.85** | **57.21** | **38.09** | **50.51** |
| w/o Fine-tuned LLM | 12.87 | 30.89 | 14.67 | 26.81 |
| w/o Protein sequence | 31.88 | 52.90 | 33.43 | 46.17 |
| w/o Early-fusing text | 33.25 | 54.88 | 35.26 | 47.90 |

</figure>

<figure data-type="table">

**Table 5.** Result in UniProtQA dataset, the best performances are marked in bold.

| Model | BLEU-2 | ROUGE-1 | ROUGE-2 | ROUGE-L |
| --- | --- | --- | --- | --- |
| LLaMA3-8B-Instruct | 2.41 | 13.66 | 1.10 | 9.07 |
| BioMedGPT-LM-10B | 5.80 | **15.88** | 7.32 | 14.68 |
| Prot2Chat (Zero-shot) | 2.23 | 14.73 | 1.73 | 10.10 |
| Prot2Chat (Fine-tuned) | **6.72** | 15.71 | **9.25** | **15.57** |

</figure>

To be specific, the Decoderlayer combines the node embeddings, the output features of the encoder, and the sequence embeddings to construct the context features for decoding through neighborhood feature aggregation. Using the message passing mechanism, the hidden state  $h_V$  is continuously

updated through multilayer stacking to simulate the dynamic generation process of the decoder.

On this basis, we further initialized  $h_V$  with the off-the-shelf sequence embedding to make the sequence and structural

<figure data-type="table">

**Table 6.** Results of KIMI evaluation, "Avg." represents the average ranking.

| Model | 1st | 2nd | 3rd | 4th | Avg. |
| --- | --- | --- | --- | --- | --- |
| Prot2Chat | 386 | 242 | 13 | 9 | 1.45 |
| BioMedGPT-LM-10B | 26 | 57 | 326 | 241 | 3.20 |
| LLaMA3-FT with sequence | 18 | 22 | 252 | 358 | 3.46 |
| Evola-10B | 220 | 329 | 59 | 42 | 1.88 |

</figure>

<figure data-type="table">

**Table 7.** Results of expert manual evaluation, "Avg." represents the average ranking.

| Model | 1st | 2nd | 3rd | 4th | Avg. |
| --- | --- | --- | --- | --- | --- |
| Prot2Chat | 359 | 266 | 17 | 8 | 1.49 |
| BioMedGPT-LM-10B | 28 | 58 | 334 | 230 | 3.18 |
| LLaMA3-FT with sequence | 11 | 21 | 239 | 379 | 3.52 |
| Evola-10B | 252 | 305 | 60 | 33 | 1.81 |

</figure>

<figure data-type="image">

![Fig. 3](images/f76f0f5dfff2511b51f6b875867c029f_img.jpg)

<synthetic_caption>
Diagram of the Text-Aware Protein-Text Adapter. A Question Vector (orange bar) and Learnable Queries (green, blue, red, purple squares) are combined and fed into a Cross-Attention module. A Protein Embedding (green grid) is fed into the Cross-Attention module as keys (k) and values (v). The output of the Cross-Attention module is a Soft Prompt (yellow and green grid).
</synthetic_caption>

**Fig. 3.** Detail of the Text-Aware Protein-Text Adapter. The adapter takes protein embedding and question vector as inputs. It integrates text information at an early stage through a set of learnable queries. Subsequently, using these queries, the protein embedding is used as keys/values for cross-attention calculations. This cross-attention module compresses the protein information into a fixed length and captures the key protein information according to the queries infused with question text information, resulting in soft prompt to assist the LLM in generating more accurate responses.

</figure>

information early-fused. Specifically, Equation 2 is replaced by:

$$h_V = \text{Embedding}(S) \quad (6)$$

In particular, all of the model weights we used are from ProteinMPNN, with no need for additional training. The final protein node vector  $h_V$  is used as the protein feature embedding for the text-aware protein-text adapter. Inspired by InstructPLM [21], we concatenate the protein representations encoded by nine released ProteinMPNN models. As a result,  $D_c = 128 * 9 = 1152$ .

### Text-Aware Protein-Text Adapter

We implemented a text-aware protein-text adapter to semantically align the information obtained from the protein encoder with natural language, incorporating question text information simultaneously. Subsequently, this fused information is provided as a soft prompt to the LLM. The protein embedding  $h_V$  obtained from the modified ProteinMPNN model is output through the linear projection layer, positional encoding and the cross-attention mechanism. During the above-mentioned process, text information is fused simultaneously, as shown in Figure 3.

To be specific, the input protein feature first passes through a linear projection layer  $W_{\text{proj}}$ , the aim is to transform the

input features to the target output dimension  $D_o$ :

$$X_{\text{proj}} = W_{\text{proj}} \cdot h_V + PE \quad (7)$$

where  $PE$  is the Dynamic Positional Encoding [27] used to capture the positional information of amino acids.

Meanwhile, we utilize the question text and the LLM to obtain the hidden state  $Qh_t$  of the last text token as the question vector, so as to achieve prompt compression and carry out the early-fusion of text and protein information, enabling the model to perceive question text information earlier:

$$Qh_t = \text{LLM}(\text{question}).\text{hiddenstate} \quad (8)$$

The complete protein encoding is too long. We adopt the idea of BLIP-2 to extract the important semantic features from it. In particular, we introduce  $n_q$  learnable queries  $Q \in \mathbb{R}^{n_q \times D_o}$ , incorporate the question vector  $Qh_t$  obtained from the question text, and also apply the positional encoding (PE) to them.

Then the multi-head cross-attention layer is adapted to capture key protein information based on queries:

$$A^k = \text{softmax} \left( \frac{Q^k K^{k\top}}{\sqrt{D_k}} \right) V^k \quad (9)$$

where  $Q^k = QW_Q^k$ ,  $K^k = X_{\text{proj}}W_K^k$  and  $V^k = X_{\text{proj}}W_V^k$  stand for queries, keys and values. Among them,  $W_Q^k, W_K^k, W_V^k$  are related parameters,  $k$  is the head index.

Then, the outputs of multi-head cross-attention are concatenated across heads and linearly transformed by  $W_{\text{out}}$  into the final protein prompt integrated with text information, which serves as the soft prompt for the LLM, where  $M$  is the number of attention heads:

$$X_{\text{protein}} = \text{Concat}(A^1, A^2, \dots, A^M)W_{\text{out}} \quad (10)$$

#### LLM Decoder

We combine  $X_{\text{protein}}$ , which integrates text and protein information, with the text question, and then input this combination into the existing LLM to obtain a response.

$$\text{response} = \text{LLM}(X_{\text{protein}}, \text{question}) \quad (11)$$

To improve domain adaptability, we fine-tuned the LLM with LoRA while training the adapter. The number of adapter training parameters is 106,483,712, while the number of LLaMA3 training parameters is 3,407,872.

The total number of training parameters of the Prot2Chat is 109M. Meanwhile, the number of training parameters of BiomedGPT-10B is 3B, that of Evola-10B is approximately 1.7B, the range of trainable parameters of Prot2Text is from 256M to 898M, and that of FAPM is 188M. This also shows the advantage of our model in significantly reducing the computational cost. Besides, we adopt the CrossEntropyLoss function as common.

## Experiment

### Datasets

We evaluated the performance of our model using the Mol-Instructions [8] and UniProtQA [16] datasets. For our experiments, we utilized the protein-oriented instruction subset in Mol-Instructions, which is primarily derived from

<figure data-type="table">

**Table 8.** Case study. The bold font represents the key points of the answers, the blue font represents the correct answers of the key points, and the red font represents the incorrect answers of the key points.

| Inputs | LLaMA3-FT with sequence | Prot2Chat | Ground Truth |
| --- | --- | --- | --- |
| ![Protein structure diagram](images/fb41783a50215a0d15dff548f1a7f541_img.jpg) **[O58736]**Using the protein structure and the following protein sequence as input, assess its potential function by comparing it to known proteins with similar sequences or structures: | The provided protein sequence is approximately 240 amino acids long and shows no significant similarity to known proteins in the database. However, it can be analyzed for potential functional regions and motifs using various bioinformatic tools. Here are some observations and predictions: 1. **Transmembrane regions:** The sequence contains several regions that are **predicted to be transmembrane**, including the following: 24-45 amino acids (ILLVGMPSGSKGEVAKAFRRRGIPVI) 63-82 amino acids (MGGDAIREEEAEKRGIPKTPEGLKYVSLKVREE) | With the provided structure and amino acid sequence, the protein is expected to display **adenylate kinase activity and participate in the phosphorylation, ATP biosynthetic process, phosphorylation.** | The protein with the structure and amino acid sequence has been analyzed, and its structure suggests a potential **involvement in ATP binding.** |

</figure>

entries in the UniProtKB/Swiss-Prot database [25]. This subset encompasses tasks such as predicting protein domains, functions, and activities. Our model was predominantly trained on this dataset.

Additionally, we employed a portion of the UniProtQA dataset, introduced by BiomedGPT [16], to assess the generalization capability of our model through zero-shot evaluation. Fine-tuning was also performed on this dataset to further validate performance. UniProtQA consists of textual descriptions of proteins, including their functions and properties. The detailed sizes of our dataset splits are provided in Table 2. It is worth noting that there are significant differences in the text style and length between the UniProtQA and Mol-Instructions datasets. This directly leads to large differences in the traditional metrics of the subsequent evaluation test results. To incorporate structural and sequence information, we retrieved the corresponding PDB files for proteins listed in Swiss-Prot. These PDB files, combined with the associated questions, serve as the input to Prot2Chat.

### Baselines

We introduced the following baselines that represent protein information using sequence.

- **LLaMA3:** LLaMA3 [6] series has attracted a lot of research attention due to its outstanding capabilities in the general domain. We use LLaMA3-8B-Instruct and input the protein sequence and question to achieve zero-shot.
- **LLaMA3-FT:** We also fine-tuned LLaMA3-8B-Instruct using textual protein sequence information and used this model as a reference model.
- **BioMedGPT-LM-10B:** BioMedGPT [16] is a domain-specific LLM trained on a large selection of corpora of human scientific knowledge. The model encodes the protein sequence with the ESM-3B [14] and uses BioMedGPT-LM-7B as the decoder to generate the response.
- **Evola-10B:** Evola [34] is trained on hundreds of millions of protein Q&A pairs and an AI-generated dataset of 150 billion words. It adopts Retrieval-Augmented Generation (RAG) to integrate external knowledge and performs excellently in generating accurate, detailed, and context-relevant answers about protein functions, providing expert-level insights for proteomics and functional genomics

research. Here, we use its model with 10B parameters as the comparative model.

- **KIMI:** We used the online large model KIMI as a control.

In order to make a fair comparison, we used the same input template for each model through prompt engineering, so that the model could output the answer as we instructed. Some other open-source models in related fields, such as FAPM, Prot2Text, and InstructProtein, were not included in the comparison because these models complete protein Q&A tasks based on classification, or there are differences in the formats of the model's inputs and outputs, as shown in Table 1. Therefore, it is hard to conduct comparisons. In addition, the ProtChatGPT model was not included in the comparison because accessible resources for this model could not be found.

### Model Setting

We jointly trained the text-aware protein-text adapter and LLaMA3 on the Mol-Instructions dataset, employing full training for the adapter and LoRA fine-tuning for LLaMA3. Inspired by InstructPLM [21], we configured the adapter with 256 queries and integrate text information on each query. For the LoRA setup, we set the rank  $r$  to 8, LoRA alpha to 16, and targeted the "q-proj" and "v-proj" modules, with a LoRA dropout rate of 0.1. The adapter comprises 106,483,712 trainable parameters, while LLaMA3 has 3,407,872 trainable parameters. We utilized the Adam optimizer and implemented gradient accumulation to optimize the training process. The initial learning rate was set to  $10^{-4}$ , with a batch size of 2 and a maximum context length of 1024 tokens, which includes both the question and answer text. The model was fine-tuned for 2 epochs, with each training session requiring approximately 1600 hours on an NVIDIA RTX 3090 GPU.

### Evaluation Metrics

To evaluate the effectiveness of the model's text generation, we employed performance metrics including BLEU [19] and ROUGE [13]. Additionally, the target text and the texts generated by different models are simultaneously input into the online KIMI model, using the following prompts: **Please select the sentence that is closest and most accurate in meaning to the Target sentence and rank sentences A,**

**B, C and D accordingly, with the first place meaning the most identical and accurate in meaning to the Target. Only provide the answer, for example, ‘B D A C’ or ‘A C D B’, etc. Target: {Ground Truth} A: {Model1 Generated Text} B: {Model2 Generated Text} C: {Model3 Generated Text} D: {Model4 Generated Text}** This allowed us to determine which generated answer was closer to the target text, thereby assisting in the evaluation of the model’s output quality. Furthermore, we conducted expert manual evaluations, where professional biology PhDs ranked the responses of different models based on their alignment with the target text.

### Main Results

The results of the assessment are shown in Table 3 to Table 7. The BLEU-2 and ROUGE-L scores of Prot2Chat exceed those of models such as BiomedGPT and Evola. The results show that there is a significant modal gap between the protein sequence and the natural language, and it is incomprehensible to directly input the protein sequence as text and problem into the LLM, resulting in the disordered and meaningless response of the model. Aligning protein language with human language is an effective way to solve this problem. Moreover, we have observed that the performance of the model trained through the early-fusion of structural information and sequences is significantly superior to that of other models leveraging either sequences or structures. This suggests that we can directly use the structure and sequence information of proteins to help language models better understand and generate technical terms through feature space alignment. In addition, the early-fusion of text information and protein information also demonstrates a significant effect. Unification of multimodal protein information and establishment of a connection between proteins and natural language can provide guidance for researchers in their study of unknown proteins. The results of our model’s prediction on the UniProtQA [16] dataset verify the generalization performance of our model. On the other hand, the evaluation results of online large language models, as shown in Table 6, demonstrate that our model outperforms other comparative models overall and the results of expert manual evaluations (Table 7) are consistent with those obtained using KIMI. Further illustrating the role of our model in protein Q&A tasks.

### Ablation Experiments

As shown in Table 4, we carried out a comparative analysis to evaluate the impact of incorporating protein structure information during the training process. Comparing with the results of the model trained with only the sequence further highlights the importance of structural information for understanding proteins. We also studied the effect of fine-tuning LLMs. The results show that training only the adapter is insufficient to achieve optimal performance. By comparing the results obtained from training in the cases of early-fusion and non-early-fusion of text and protein information, we found that the early-fusion of information plays a non-negligible role in the understanding of proteins and the generation of large language models, and its effect is significantly better than that of late-fusion. These findings emphasize the necessity of integrating protein structure information and a unified multimodal alignment module, which enables large language models to better understand and generate domain-specific technical terms.

### Case Study

As shown in Table 8, the results obtained from our use of the protein Q&A test dataset indicate that the responses generated by the model trained by adding protein structure and early-fusing text information are significantly superior to those of other trained models (taking LLaMA3-FT as an example here). This further demonstrates that protein structure is of crucial importance for the comprehensive understanding of proteins, the early-fusion and alignment of multimodal information plays a non-negligible role in the understanding of proteins and the generation of large language models.

## Conclusion

This paper introduces a novel protein Q&A model that addresses the limitations of traditional protein Q&A tasks by leveraging the modified ProteinMPNN and a text-aware adapter module to integrate protein structure and sequence information with natural language text. By harnessing the flexible generation capabilities of large language models (LLMs), our approach effectively bridges the gap between protein data and textual understanding. Experimental results demonstrate that the model trained by adding protein structure and early-fusing text information significantly outperforms other baseline models. This not only highlights the crucial role of protein structure in protein understanding and analysis but also proves the effectiveness of the early-fusion of protein information and text. Furthermore, the results validate the effectiveness and generalization ability of our model in protein Q&A tasks.

## References

1. Hadi Abdine, Michail Chatzianastasis, Costas Bouyioukos, and Michalis Vazirgiannis. Prot2text: Multimodal protein’s function generation with gnns and transformers. In *Proceedings of the AAAI Conference on Artificial Intelligence*, volume 38, pages 10757–10765, 2024.
2. Josh Abramson, Jonas Adler, Jack Dunger, Richard Evans, Tim Green, Alexander Pritzel, Olaf Ronneberger, Lindsay Willmore, Andrew J Ballard, Joshua Bambrick, et al. Accurate structure prediction of biomolecular interactions with alphafold 3. *Nature*, pages 1–3, 2024.
3. Samir Char, Nathaniel Corley, Sarah Alamdari, Kevin K Yang, and Ava P Amini. Protnote: a multimodal method for protein-function annotation. *bioRxiv*, pages 2024–10, 2024.
4. Justas Dauparas, Ivan Anishchenko, Nathaniel Bennett, Hua Bai, Robert J Ragotte, Lukas F Milles, Basile IM Wicky, Alexis Courbet, Rob J de Haas, Neville Bethel, et al. Robust deep learning–based protein sequence design using proteinmpnn. *Science*, 378(6615):49–56, 2022.
5. Danny Driess, Fei Xia, Mehdi SM Sajjadi, Corey Lynch, Aakanksha Chowdhery, Brian Ichter, Ayzaan Wahid, Jonathan Tompson, Quan Vuong, Tianhe Yu, et al. Palm-e: An embodied multimodal language model. *arXiv preprint arXiv:2303.03378*, 2023.
6. Abhimanyu Dubey, Abhinav Jauhri, Abhinav Pandey, Abhishek Kadian, Ahmad Al-Dahle, Aiesha Letman, Akhil Mathur, Alan Schelten, Amy Yang, Angela Fan, et al. The llama 3 herd of models. *arXiv preprint arXiv:2407.21783*, 2024.

7. Junfeng Fang, Shuai Zhang, Chang Wu, Zhengyi Yang, Zhiyuan Liu, Sihang Li, Kun Wang, Wenjie Du, and Xiang Wang. Molc: Towards molecular relational modeling in language models. *arXiv preprint arXiv:2402.03781*, 2024.
8. Yin Fang, Xiaozhuan Liang, Ningyu Zhang, Kangwei Liu, Rui Huang, Zhuo Chen, Xiaohui Fan, and Huajun Chen. Mol-instructions: A large-scale biomolecular instruction dataset for large language models. *arXiv preprint arXiv:2306.08018*, 2023.
9. Vladimir Gligorijević, P Douglas Renfrew, Tomasz Koscierek, Julia Koehler Leman, Daniel Berenberg, Tommi Vatanen, Chris Chandler, Bryn C Taylor, Ian M Fisk, Hera Vlamakis, et al. Structure-based protein function prediction using graph convolutional networks. *Nature communications*, 12(1):3168, 2021.
10. Edward J Hu, Yelong Shen, Phillip Wallis, Zeyuan Allen-Zhu, Yuanzhi Li, Shean Wang, Lu Wang, and Weizhu Chen. LoRA: Low-rank adaptation of large language models. In *International Conference on Learning Representations*, 2022.
11. Maxat Kulmanov and Robert Hoehndorf. Deepgopius: improved protein function prediction from sequence. *Bioinformatics*, 36(2):422–429, 2020.
12. Junnan Li, Dongxu Li, Silvio Savarese, and Steven Hoi. Blip-2: Bootstrapping language-image pre-training with frozen image encoders and large language models. In *International conference on machine learning*, pages 19730–19742. PMLR, 2023.
13. Chin-Yew Lin. ROUGE: A package for automatic evaluation of summaries. In *Text Summarization Branches Out*, pages 74–81, Barcelona, Spain, July 2004. Association for Computational Linguistics.
14. Zeming Lin, Halil Akin, Roshan Rao, Brian Hie, Zhongkai Zhu, Wenting Lu, Allan dos Santos Costa, Maryam Fazel-Zarandi, Tom Sercu, Sal Candido, et al. Language models of protein sequences at the scale of evolution enable accurate structure prediction. *BioRxiv*, 2022:500902, 2022.
15. Zeming Lin, Halil Akin, Roshan Rao, Brian Hie, Zhongkai Zhu, Wenting Lu, Nikita Smetanin, Robert Verkuil, Ori Kabeli, Yaniv Shmueli, et al. Evolutionary-scale prediction of atomic-level protein structure with a language model. *Science*, 379(6637):1123–1130, 2023.
16. Yizhen Luo, Jiahuan Zhang, Siqi Fan, Kai Yang, Yushuai Wu, Mu Qiao, and Zaiqing Nie. Biomedgpt: Open multimodal generative pre-trained transformer for biomedicine. *arXiv preprint arXiv:2308.09442*, 2023.
17. Chenyang Lyu, Minghao Wu, Longyue Wang, Xinting Huang, Bingshuai Liu, Zefeng Du, Shuming Shi, and Zhaopeng Tu. Macaw-llm: Multi-modal language modeling with image, audio, video, and text integration. *arXiv preprint arXiv:2306.09093*, 2023.
18. Pascal Notin, Mafalda Dias, Jonathan Frazer, Javier Marchena-Hurtado, Aidan N Gomez, Debora Marks, and Yarin Gal. Tranception: protein fitness prediction with autoregressive transformers and inference-time retrieval. In *International Conference on Machine Learning*, pages 16990–17017. PMLR, 2022.
19. Kishore Papineni, Salim Roukos, Todd Ward, and Wei-Jing Zhu. Bleu: a method for automatic evaluation of machine translation. In Pierre Isabelle, Eugene Charniak, and Dekang Lin, editors, *Proceedings of the 40th Annual Meeting of the Association for Computational Linguistics*, pages 311–318, Philadelphia, Pennsylvania, USA, July 2002. Association for Computational Linguistics.
20. Ruoyu Qin, Zheming Li, Weiran He, Mingxing Zhang, Yongwei Wu, Weimin Zheng, and Xinran Xu. Mooncake: A kvcache-centric disaggregated architecture for llm serving. *arXiv preprint arXiv:2407.00079*, 2024.
21. Jiezhuo Qiu, Junde Xu, Jie Hu, Hanqun Cao, Liya Hou, Zijun Gao, Xinyi Zhou, Anni Li, Xiujuan Li, Bin Cui, et al. Instructplm: Aligning protein language models to follow protein structure instructions. *bioRxiv*, pages 2024–04, 2024.
22. Nathaniel Smith, Xinyu Yuan, Chesney Melissinos, and Gaurav Moghe. Funcfetch: an llm-assisted workflow enables mining thousands of enzyme–substrate interactions from published manuscripts. *Bioinformatics*, 41(1):btae756, 2025.
23. Jin Su, Chenchen Han, Yuyang Zhou, Junjie Shan, Xibin Zhou, and Fajie Yuan. Saprot: Protein language modeling with structure-aware vocabulary. *bioRxiv*, pages 2023–10, 2023.
24. Ross Taylor, Marcin Kardas, Guillem Cucurull, Thomas Scialom, Anthony Hartshorn, Elvis Saravia, Andrew Poulton, Viktor Kerkez, and Robert Stojnic. Galactica: A large language model for science. *arXiv preprint arXiv:2211.09085*, 2022.
25. The UniProt Consortium. Uniprot: the universal protein knowledgebase. *Nucleic acids research*, 46(5):2699–2699, 2018.
26. Michel van Kempen, Stephanie S Kim, Charlotte Tumescheit, Milot Mirdita, Cameron LM Gilchrist, Johannes Söding, and Martin Steinegger. Foldseek: fast and accurate protein structure search. *Biorxiv*, pages 2022–02, 2022.
27. A Vaswani. Attention is all you need. *Advances in Neural Information Processing Systems*, 2017.
28. Chao Wang, Hehe Fan, Ruijie Quan, and Yi Yang. Protchatgpt: Towards understanding proteins with large language models. *arXiv preprint arXiv:2402.09649*, 2024.
29. Zeyuan Wang, Qiang Zhang, Keyan Ding, Ming Qin, Xiang Zhuang, Xiaotong Li, and Huajun Chen. Instructprotein: Aligning human and protein language via knowledge instruction. *arXiv preprint arXiv:2310.03269*, 2023.
30. Hannah K Wayment-Steele, Adedolapo Ojoawo, Renee Otten, Julia M Apitz, Warintra Pitsawong, Marc Hömberger, Sergey Ovchinnikov, Lucy Colwell, and Dorothee Kern. Predicting multiple conformations via sequence clustering and alphafold2. *Nature*, 625(7996):832–839, 2024.
31. Wenkai Xiang, Zhaoping Xiong, Huan Chen, Jiacheng Xiong, Wei Zhang, Zunyun Fu, Mingyue Zheng, Bing Liu, and Qian Shi. Fapm: Functional annotation of proteins using multi-modal models beyond structural modeling. *Bioinformatics*, page btae680, 2024.
32. Hang Zhang, Xin Li, and Lidong Bing. Video-llama: An instruction-tuned audio-visual language model for video understanding. *arXiv preprint arXiv:2306.02858*, 2023.
33. Hongjian Zhou, Fenglin Liu, Boyang Gu, Xinyu Zou, Jinfa Huang, Jinge Wu, Yiru Li, Sam S Chen, Peilin Zhou, Junling Liu, et al. A survey of large language models in medicine: Progress, application, and challenge. *arXiv preprint arXiv:2311.05112*, 2023.
34. Xibin Zhou, Chenchen Han, Yingqi Zhang, Jin Su, Kai Zhuang, Shiyu Jiang, Zichen Yuan, Wei Zheng, Fengyuan Dai, Yuyang Zhou, et al. Decoding the molecular language of proteins with evola. *bioRxiv*, pages 2025–01, 2025.
