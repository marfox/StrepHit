#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import json
import logging
from random import choice
from sys import exit

import click
from nltk import RegexpParser
from nltk.parse.stanford import StanfordParser
from nltk.tree import Tree

from strephit.commons.io import load_scraped_items
from strephit.commons.split_sentences import PunktSentenceSplitter
from strephit.commons import parallel

logger = logging.getLogger(__name__)


class SentenceExtractor:
    """ Base class for sentence extractors.
    """

    def __init__(self, corpus, pos_tag_key, document_key, sentences_key, language, lemma_to_token, match_base_form):
        """ Initializes the extractor.

            :param corpus: The corpus, iterable of `dict`s. Generator preferred
            :param pos_tag_key: The key from which to retrieve the pos tagged document
            :param document_key: The key from which to retrieve the textual document
            :param sentences_key: The key to which the extracted sentences should be stored
            :param language: The language the text is in
            :param lemma_to_token: Mapping from lemma to list of tokens
        """
        self.corpus = corpus
        self.pos_tag_key = pos_tag_key
        self.sentences_key = sentences_key
        self.document_key = document_key
        self.lemma_to_token = lemma_to_token
        self.language = language
        self.lemma_to_token = lemma_to_token if match_base_form else self._filter_base_form(lemma_to_token)

    def extract_from_item(self, item):
        """ Extract sentences from an item. Relies on `setup_extractor`
            having been called

            :param dict item: Item from which to extract sentences
            :return: The original item and list of extracted sentences
            :rtype: tuple of dict, list
        """
        raise NotImplementedError()

    def setup_extractor(self):
        """ Optional setup code, run before starting the extraction
        """
        pass

    def teardown_extractor(self):
        """ Optional teardown code, run after the extraction
        """
        pass

    def extract(self, processes=0):
        """ Processes the corpus extracting sentences from each item
            and storing them in the item itself.
        """
        self.setup_extractor()

        try:
            count = 0
            for i, (item, extracted) in enumerate(parallel.map(self.extract_from_item,
                                                               self.corpus, processes)):

                # assign an unique incremental ID to each sentence
                for each in extracted:
                    each['id'] = count
                    each['url'] = item['url']
                    count += 1

                    yield each

                if (i + 1) % 10000 == 0:
                    logger.info('Processed %d items, extracted %d sentences',
                                i + 1, count)

            logger.info('Total sentences extracted: %d', count)
        finally:
            self.teardown_extractor()

    @staticmethod
    def _filter_base_form(lemma_to_token):
        """ Remove the base form from each list of tokens """
        for lemma, tokens in lemma_to_token.iterite:
            if lemma in tokens:
                tokens.remove(lemma)
        return lemma_to_token


class OneToOneExtractor(SentenceExtractor):
    """ 121 extraction strategy: 1 sentence per 1 LU
        N.B.: the same sentence will appear only once
        the sentence is assigned to a RANDOM LU
    """
    splitter = None
    all_verb_tokens = None
    token_to_lemma = None

    def setup_extractor(self):
        self.splitter = PunktSentenceSplitter(self.language)

        self.all_verb_tokens = set()
        self.token_to_lemma = {}
        for lemma, match_tokens in self.lemma_to_token.iteritems():
            for match_token in match_tokens:
                self.all_verb_tokens.add(match_token.lower())
                self.token_to_lemma[match_token.lower()] = lemma
        logger.debug("All match tokens: %s" % self.all_verb_tokens)

    def extract_from_item(self, item):
        extracted = []
        url = item.get('url')
        if not url:
            logger.warn('skipping item without url')
            return

        tagged = item.get(self.pos_tag_key)
        if not tagged:
            logger.warn('skipped item')
            return

        sentences = self.splitter.split_tokens([token for token, pos, lemma in tagged])
        tokens = 0

        for sentence in sentences:
            # retrieve POS tags of this sentence
            tags = tagged[tokens:tokens + len(sentence)]
            tokens += len(sentence)

            sentence_verbs = [token for token, pos, lemma in tags if pos.startswith('V')]

            matched = []
            for token in self.all_verb_tokens:
                if token in sentence_verbs:
                    matched.append(token)

            if matched:
                assigned_token = choice(matched)
                assigned_lu = self.token_to_lemma[assigned_token]
                extracted.append({
                    'lu': assigned_lu,
                    'text': ' '.join(sentence),
                    'tagged': tags,
                    'url': url,
                })

        if extracted:
            logger.debug("%d sentences extracted", len(extracted))
            return item, extracted
        else:
            logger.debug("No sentences extracted")


class ManyToManyExtractor(SentenceExtractor):
    """ n2n extraction strategy: many sentences per many LUs
        N.B.: the same sentence is likely to appear multiple times
    """
    splitter = None

    def setup_extractor(self):
        self.splitter = PunktSentenceSplitter(self.language)

    def extract_from_item(self, item):
        extracted = []
        text = item.get(self.document_key)
        url = item.get('url')
        if not text or not url:
            logger.warn('skipping item without url or bio')
            return

        sentences = self.splitter.split(text)
        datatxt_links = item.get('nc:contentInfo', {}).get('nc:companyTXTInfo', {}).get('rnews:articleBody', {}).get('annotations', [])
        subject_links = [x for x in item.get('nc:annotations', {}).get('nc:subjectAnnotation', [])
                         if 'nc:offsets' in x]
        for each in subject_links:
            each['start'], each['end'] = each.pop('nc:offsets')

        all_links = sorted(datatxt_links + subject_links, key=lambda x: x['start'])

        cursor = 0
        for sentence in sentences:
            # move cursor to start of this sentence
            cursor += text.index(sentence)
            text = text[text.index(sentence):]

            link_counts = 0
            for each in all_links:
                if each['end'] <= cursor + len(sentence):
                    link_counts += 1
                else:
                    break

            this_links, all_links = all_links[:link_counts], all_links[link_counts:]
            for each in this_links:
                each['start'] -= cursor
                each['end'] -= cursor

            # move cursor to end of this sentence
            cursor += len(sentence)
            text = text[len(sentence):]

            if len(sentence.split()) > 25:
                continue

            for lemma, match_tokens in self.lemma_to_token.iteritems():
                for match in match_tokens:
                    if match.lower() in sentence:
                        extracted.append({
                            'url': url,
                            'lu': lemma,
                            'text': sentence,
                            'links': this_links,
                        })

        if extracted:
            logger.debug("%d sentences extracted", len(extracted))
            return item, extracted
        else:
            logger.debug("No sentences extracted")


class SyntacticExtractor(SentenceExtractor):
    """ Tries to split sentences into sub-sentences so that each of them
        contains only one LU
    """

    splitter = None
    parser = None
    token_to_lemma = None
    all_verbs = None

    def setup_extractor(self):
        self.splitter = PunktSentenceSplitter(self.language)
        self.parser = StanfordParser(path_to_jar='dev/stanford-corenlp-3.6.0.jar',
                                     path_to_models_jar='dev/stanford-corenlp-3.6.0-models.jar',
                                     java_options=' -mx2G -Djava.ext.dirs=dev/')

        self.token_to_lemma = {}
        for lemma, tokens in self.lemma_to_token.iteritems():
            for t in tokens:
                self.token_to_lemma[t] = lemma
        self.all_verbs = set(self.token_to_lemma.keys())

    def extract_from_item(self, item):
        extracted = []
        bio = item.get(self.document_key, '').lower()
        url = item.get('url')
        if not bio or not url:
            logger.warn('skipping item without url or bio')
            return

        try:
            roots = self.parser.raw_parse_sents(self.splitter.split(bio))
        except (OSError, UnicodeDecodeError):
            logger.exception('cannot parse biography, skipping')
            return

        for root in roots:
            root = root.next()
            try:
                sub_sents = self.find_sub_sentences(root)
            except:
                logger.exception('cannot find sub-sentences')
                continue

            for sub in sub_sents:
                try:
                    text = ' '.join(chunk for _, chunk in self.find_terminals(sub))
                    logger.debug('processing text ' + text)
                    verbs = set(chunk for _, chunk in self.find_terminals(sub, 'V'))
                except:
                    logger.exception('cannot extract verbs or parse sentence')
                    continue

                found = verbs.intersection(self.all_verbs)

                if len(found) == 0:
                    logger.debug('No matching verbs found in sub sentence')
                elif len(found) == 1:
                    extracted.append({
                        'lu': self.token_to_lemma[found.pop()],
                        'text': text,
                        'url': url,
                    })
                else:
                    logger.debug('More than one matching verbs found in sentence %s: %s',
                                 text, repr(found))

        if extracted:
            logger.debug("%d sentences extracted...", len(extracted))
            return item, extracted
        else:
            logger.debug("No sentences extracted. Skipping the whole item ...")

    def find_sub_sentences(self, tree):
        # sub-sentences are the lowest S nodes in the parse tree
        if not isinstance(tree, Tree):
            return []

        s = reduce(lambda x, y: x + y, map(self.find_sub_sentences, iter(tree)), [])
        if tree.label() == 'S':
            return s or [tree]
        else:
            return s

    def find_terminals(self, tree, label=None):
        # finds all terminals in the tree with the given label prefix
        if len(tree) == 1 and not isinstance(tree[0], Tree):
            if label is None or tree.label().startswith(label):
                yield (tree.label(), tree[0])
        else:
            for child in tree:
                for each in self.find_terminals(child, label):
                    yield each


class GrammarExtractor(SentenceExtractor):
    """ Grammar-based extraction strategy: pick sentences that comply with a pre-defined grammar. """

    splitter = None
    parser = None
    # Grammars rely on POS labels, which are language-dependent
    grammars = {
        'en': r"""
                NOPH: {<PDT>?<DT|PP.*|>?<CD>?<JJ.*|VVN>*<N.+|FW>+<CC>?}
                CHUNK: {<NOPH>+<MD>?<V.+>+<IN|TO>?<NOPH>+}
               """,
        'it': r"""
                SN: {<PRO.*|DET.*|>?<ADJ>*<NUM>?<NOM|NPR>+<NUM>?<ADJ|VER:pper>*}
                CHUNK: {<SN><VER.*>+<SN>}
               """,
    }

    def setup_extractor(self):
        self.splitter = PunktSentenceSplitter(self.language)
        grammar = self.grammars.get(self.language)
        if grammar:
            self.parser = RegexpParser(grammar)
        else:
            raise ValueError(
                "Invalid or unsupported language: '%s'. Please use one of the currently supported ones: %s" % (
                    self.language, self.grammars.keys())
            )

        for lemma, match_tokens in self.lemma_to_token.iteritems():
            self.lemma_to_token[lemma] = set([match.lower() for match in match_tokens])

    def extract_from_item(self, item):
        extracted = []
        url = item.get('url')
        if not url:
            logger.warn('skipping item without url')
            return

        tagged = item.get(self.pos_tag_key)
        if not tagged:
            return

        # Sentence splitting
        sentences = self.splitter.split_tokens([token for token, pos, lemma in tagged])
        tokens = 0
        for sentence in sentences:
            tags = tagged[tokens:tokens + len(sentence)]
            tokens += len(sentence)

            # Parsing via grammar
            parsed = self.parser.parse([(token, pos) for token, pos, lemma in tags])

            # Loop over sub-sentences that match the grammar
            for grammar_match in parsed.subtrees(lambda t: t.label() == 'CHUNK'):
                logger.debug("Grammar match: '%s'" % grammar_match)
                # Look up the LU
                for token, pos in grammar_match.leaves():
                    # Restrict match to sub-sentence verbs only
                    if pos.startswith('V'):
                        for lemma, match_tokens in self.lemma_to_token.iteritems():
                            if token.lower() in match_tokens:
                                # Return joined chunks only
                                # TODO test with full sentence as well
                                # TODO re-constitute original text (now join on space)
                                text = ' '.join([leaf[0] for leaf in grammar_match.leaves()])
                                logger.debug("Extracted sentence: '%s'" % text)
                                logger.debug("Sentence token '%s' is in matches %s" % (token, match_tokens))
                                logger.debug("Extracted sentence: %s" % text)
                                extracted.append({
                                    'lu': lemma,
                                    'text': text,
                                    'tagged': tags,
                                    'url': url,
                                })

        if extracted:
            logger.debug("%d sentences extracted. Removing the full text from the item ...", len(extracted))
            item.pop(self.document_key)
            return item, extracted
        else:
            logger.debug("No sentences extracted. Skipping the whole item ...")


def extract_sentences(corpus, pos_tag_key, sentences_key, document_key, language,
                      lemma_to_tokens, strategy, match_base_form, processes=0):
    """
    Extract sentences from the given corpus by matching tokens against a given set.

    :param corpus: Pos-tagged corpus, as an iterable of documents
    :param str sentences_key: dict key where to put extracted sentences
    :param str pos_tag_key: dict key where the pos-tagged text is
    :param str document_key: dict key where the textual document is
    :param str language: ISO 639-1 language code used for tokenization and sentence splitting
    :param dict lemma_to_tokens: Dict with corpus lemmas as keys and tokens to be matched as values
    :param str strategy: One of the 4 extraction strategies ['121', 'n2n', 'grammar', 'syntactic']
    :param bool match_base_form: whether to match verbs base form
    :param int processes: How many concurrent processes to use
    :return: the corpus, updated with the extracted sentences and the number of extracted sentences
    :rtype: generator of tuples
    """

    if strategy == 'n2n':
        logger.info("Will extract sentences using the 'many to many' strategy: the same "
                    "sentence is likely to appear multiple times, with different LUs.")
        extractor = ManyToManyExtractor
    elif strategy == '121':
        logger.info("Will extract sentences using the 'one to one' strategy: the same "
                    "sentence will appear only once.")
        extractor = OneToOneExtractor
    elif strategy == 'grammar':
        logger.info("Will extract sentences using the 'grammar' strategy: the same "
                    "sentence will appear only once.")
        extractor = GrammarExtractor
    elif strategy == 'syntactic':
        logger.info("Will extract sentences using the 'syntactic' strategy: the same "
                    "sentence will appear only once.")
        extractor = SyntacticExtractor
    else:
        raise ValueError("Malformed or unsupported extraction strategy: "
                         "please use one of ['121', 'n2n', 'grammar', or 'syntactic']")

    for each in extractor(corpus, pos_tag_key, document_key, sentences_key, language,
                          lemma_to_tokens, match_base_form).extract(processes):
        yield each


@click.command()
@click.argument('pos_tagged', type=click.Path(exists=True))
@click.argument('language_code')
@click.argument('lemma_to_tokens', type=click.File('r'))
@click.option('--strategy', '-s', type=click.Choice(['n2n', '121', 'grammar', 'syntactic']), default='n2n')
@click.option('--output', '-o', type=click.File('w'), default='dev/sentences.jsonlines')
@click.option('--sentences-key', default='sentences')
@click.option('--pos-tag-key', default='pos_tag')
@click.option('--document-key', default='bio')
@click.option('--processes', '-p', default=0)
@click.option('--match-base-form', is_flag=True, default=False)
def main(pos_tagged, language_code, lemma_to_tokens, strategy, output, processes,
         sentences_key, pos_tag_key, document_key, match_base_form):
    """ Extract corpus sentences containing at least one token in the given set. """
    corpus = load_scraped_items(pos_tagged)
    updated = extract_sentences(corpus, pos_tag_key, sentences_key, document_key, language_code,
                                json.load(lemma_to_tokens), strategy, match_base_form, processes)
    for item in updated:
        output.write(json.dumps(item) + '\n')
    return 0


if __name__ == '__main__':
    exit(main())
