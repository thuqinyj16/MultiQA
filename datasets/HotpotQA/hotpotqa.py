import json
from datasets.multiqa_dataset import MultiQA_DataSet
from overrides import overrides
from allennlp.common.file_utils import cached_path
import tqdm
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import pairwise_distances
from nltk.corpus import stopwords
import string

class NltkPlusStopWords():
    """ Configurablable access to stop word """

    def __init__(self, punctuation=False):
        self._words = None
        self.punctuation = punctuation

    @property
    def words(self):
        if self._words is None:
            self._words = set(stopwords.words('english'))
            # Common question words we probably want to ignore, "de" was suprisingly common
            # due to its appearance in person names
            self._words.update(["many", "how", "de"])
            if self.punctuation:
                self._words.update(string.punctuation)
                self._words.update(["£", "€", "¥", "¢", "₹", "\u2212",
                                    "\u2014", "\u2013", "\ud01C", "\u2019", "\u201D", "\u2018", "\u00B0"])
        return self._words


class Paragraph_TfIdf_Scoring():
    # Hard coded weight learned from a logistic regression classifier
    TFIDF_W = 5.13365065
    LOG_WORD_START_W = 0.46022765
    FIRST_W = -0.08611607
    LOWER_WORD_W = 0.0499123
    WORD_W = -0.15537181

    def __init__(self):
        self._stop = NltkPlusStopWords(True).words
        self._stop.remove('퀜')
        self._tfidf = TfidfVectorizer(strip_accents="unicode", stop_words=self._stop)

    def score_paragraphs(self, question, paragraphs):
        tfidf = self._tfidf
        text = paragraphs
        para_features = tfidf.fit_transform(text)
        q_features = tfidf.transform(question)

        q_words = {x for x in question if x.lower() not in self._stop}
        q_words_lower = {x.lower() for x in q_words}
        word_matches_features = np.zeros((len(paragraphs), 2))
        for para_ix, para in enumerate(paragraphs):
            found = set()
            found_lower = set()
            for sent in para:
                for word in sent:
                    if word in q_words:
                        found.add(word)
                    elif word.lower() in q_words_lower:
                        found_lower.add(word.lower())
            word_matches_features[para_ix, 0] = len(found)
            word_matches_features[para_ix, 1] = len(found_lower)

        tfidf = pairwise_distances(q_features, para_features, "cosine").ravel()
        # TODO 0 represents if this paragraph start a real paragraph (number > 0 represents the
        # paragraph was split. when we split paragraphs we need to take care of this...
        starts = np.array([0 for p in paragraphs])
        log_word_start = np.log(starts/400.0 + 1)
        first = starts == 0
        scores = tfidf * self.TFIDF_W + self.LOG_WORD_START_W * log_word_start + self.FIRST_W * first +\
                 self.LOWER_WORD_W * word_matches_features[:, 1] + self.WORD_W * word_matches_features[:, 0]
        return scores


class HotpotQA(MultiQA_DataSet):
    """

    """

    def __init__(self):
        self.DATASET_NAME = 'HotpotQA'

    @overrides
    def build_header(self, contexts, split, preprocessor, dataset_version, dataset_flavor):
        header = {
            "dataset_name": self.DATASET_NAME,
            "split": split,
            "dataset_url": "https://hotpotqa.github.io/",
            "license": "http://creativecommons.org/licenses/by-sa/4.0/legalcode",
            "data_source": "Wikipedia",
            "context_answer_detection_source": "MultiQA",
            "tokenization_source": "MultiQA",
            "full_schema": super().compute_schema(contexts),
            "text_type": "abstract",
            "number_of_qas": sum([len(context['qas']) for context in contexts]),
            "number_of_contexts": len(contexts),
            "readme": "",
            "multiqa_version": super().get_multiqa_version()
        }

        return header

    @overrides
    def format_predictions(self, predictions):
        return {"answer": predictions, "sp": {}}

    @overrides
    def build_contexts(self, split, preprocessor, sample_size, dataset_version, dataset_flavor, input_file):
        if input_file is not None:
            single_file_path = cached_path(input_file)
        else:
            if split == 'train':
                single_file_path = cached_path("http://curtis.ml.cmu.edu/datasets/hotpot/hotpot_train_v1.1.json")
            elif split == 'dev':
                single_file_path = cached_path("http://curtis.ml.cmu.edu/datasets/hotpot/hotpot_dev_distractor_v1.json")

        with open(single_file_path, 'r') as myfile:
            data = json.load(myfile)

        _para_tfidf_scoring = Paragraph_TfIdf_Scoring()
        contexts = []
        for example in tqdm.tqdm(data[0:10], total=len(data), ncols=80):

            # choosing only the gold paragraphs
            #gold_paragraphs = []
            #for supp_fact_title in set([supp_fact[0] for supp_fact in example['supporting_facts']]):
            #    for context in example['context']:
            #        # finding the gold context
            #        if context[0] == supp_fact_title:
            #            gold_paragraphs.append(context)

            # Arranging paragraphs by TF-IDF
            tf_idf_scores = _para_tfidf_scoring.score_paragraphs([example['question']], \
                                                [' '.join(p[1]) for p in example['context']])

            documents = []
            supporting_context = []
            for doc_id, order_id in enumerate(list(np.argsort(tf_idf_scores))):
                para = example['context'][order_id]
                # calcing the sentence_start_bytes for the supporting facts in hotpotqa
                offset = 0
                sentence_start_bytes = [0]
                for sentence in para[1]:
                    offset += len(sentence) + 1
                    sentence_start_bytes.append(offset)
                sentence_start_bytes = sentence_start_bytes[:-1]

                # choosing only the gold paragraphs
                for supp_fact in example['supporting_facts']:
                    # finding the gold context
                    if para[0] == supp_fact[0] and len(sentence_start_bytes) > supp_fact[1]:
                        supporting_context.append({'doc_id':doc_id,
                                                   'part':'text',
                                                   'start_byte': sentence_start_bytes[supp_fact[1]],
                                                   'text':para[1][supp_fact[1]]})

                # joining all sentences into one
                documents.append({'text':' '.join(para[1]) + ' ',
                                 'title': para[0],
                                 'metadata': {"text": {"sentence_start_bytes": sentence_start_bytes}}})

            if example['answer'].lower() == 'yes':
                answers = {'open-ended': {'answer_candidates': [{'yesno':{'single_answer':'yes'}}]}}
            elif example['answer'].lower() == 'no':
                answers = {'open-ended': {'answer_candidates': [{'yesno':{'single_answer':'no'}}]}}
            else:
                answers = {'open-ended': {'answer_candidates': [{'extractive': {'single_answer': {'answer': example['answer']}}}]}}


            qas = [{"qid": self.DATASET_NAME + '_q_' + example['_id'],
                    "metadata":{'type':example['type'],'level':example['level']},
                    "supporting_context": supporting_context,
                    "question": example['question'],
                    "answers": answers,
                    }]

            contexts.append({"id": self.DATASET_NAME + '_' + example['_id'],
                             "context": {"documents": documents},
                             "qas": qas})

        if sample_size != None:
            contexts = contexts[0:sample_size]

        if split == 'train':
            ans_in_supp_context = True
        else:
            ans_in_supp_context = False
        contexts = preprocessor.tokenize_and_detect_answers(contexts, search_answer_within_supp_context=ans_in_supp_context)

        return contexts