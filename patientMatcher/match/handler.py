#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
import datetime
import requests
import pymongo
import json
from werkzeug.datastructures import Headers
from patientMatcher.match.genotype_matcher import match as genomatch
from patientMatcher.match.phenotype_matcher import match as phenomatch
from patientMatcher.parse.patient import json_patient

LOG = logging.getLogger(__name__)


def patient_matches(database, patient_id, type=None, with_results=True):
    """Retrieve all matches for a patient specified by an ID

    Args:
        database(pymongo.database.Database)
        patient_id(str): ID of a patient in database
        type(str): type of matching, external or internal
        with_results(bool): if True only matches with results are returned

    Returns:
        matches(list): a list of dictionaries = [ {match_obj1}, {match_obj2}, .. ]
    """
    query = {
        '$or' : [
            {'data.patient.id' : patient_id }, # collect matches triggered by patient
            {'results.patients.patient.id' : patient_id} # and matches where patient is among results
        ]}
    if type:
        query['match_type'] = type
    if with_results:
        query['has_matches'] = True

    matches = list(database['matches'].find(query))
    LOG.info(matches)
    return matches


def internal_matcher(database, patient_obj, max_pheno_score, max_geno_score, max_results=5):
    """Handles a query patient matching against the database of patients

    Args:
        database(pymongo.database.Database)
        patient_obj(dic): a mme formatted patient object
        max_pheno_score(float): a number between 0 and 1
        max_geno_score(float): a number between 0 and 1
        max_results(int): the maximum number of results that the server should return.

    Returns:
        internal_match(dict): a matching object with results(list) sorted by score
    """
    json_pat = json_patient(patient_obj)
    pheno_matches = []
    pheno_m_keys = []
    geno_matches = []
    geno_m_keys = []
    matches = []

    # phenotype score can be obtained if patient has an associated phenotype (HPO or OMIM terms)
    if patient_obj.get('features') or patient_obj.get('disorders'):
        LOG.info('Matching phenotypes against database patients..')
        pheno_matches = phenomatch(database, max_pheno_score, patient_obj.get('features',[]), patient_obj.get('disorders',[]))
        pheno_m_keys = list(pheno_matches.keys())

    # genomic score can be obtained if patient has at least one genomic feature
    if patient_obj.get('genomicFeatures') and len(patient_obj['genomicFeatures']) > 0:
        LOG.info('Matching variants/genes against database patients..')
        geno_matches = genomatch(database, patient_obj['genomicFeatures'], max_geno_score)
        geno_m_keys = list(geno_matches.keys())

    # obtain unique list of all patient IDs returned by the 2 algorithms:
    unique_patients = list(set(pheno_m_keys + geno_m_keys))

    # create matching result objects with combined score from the 2 algorithms
    for key in unique_patients:
        pheno_score = 0
        geno_score = 0
        patient_obj = None

        if key in pheno_m_keys:
            pheno_score = pheno_matches[key]['pheno_score']
            patient_obj = pheno_matches[key]['patient_obj']

        if key in geno_m_keys:
            geno_score = geno_matches[key]['geno_score']
            patient_obj = geno_matches[key]['patient_obj']

        p_score = pheno_score + geno_score
        score = {
            'patient' : p_score,
            '_genotype' : geno_score,
            '_phenotype' : pheno_score
        }
        match = {
            'patient' : json_patient(patient_obj),
            'score' : score
        }
        matches.append(match)

    # sort patient matches by patient (combined) score
    sorted_matches = sorted(matches, key=lambda k : k['score']['patient'], reverse=True)

    # this is saved to server, regardless of the results returned by the nodes
    has_matches = False
    if sorted_matches:
        has_matches = True

    internal_match = {
        'created' : datetime.datetime.now(),
        'has_matches' : has_matches,
        'data' : {'patient' : json_pat}, # description of the patient submitted
        'results' : [{
            'node' : { 'id': 'patientMatcher', 'label': 'patientMatcher server'},
            'patients' : sorted_matches[:max_results]
        }],
        'match_type' : 'internal'
    }

    # return matching object
    return internal_match


def external_matcher(database, host, patient, node=None):
    """Handles a query patient matching against all connected MME nodes

    Args:
        database(pymongo.database.Database)
        host(str): Name of this server (MME_HOST in config file)
        patient(dict) : a MME patient entity
        node(str): id of the node to search in

    Returns:
        external_match(dict): a matching object containing a list of results in 'results' field
    """
    query = {}
    if node:
        query['_id'] = node
    connected_nodes = list(database['nodes'].find()) #get all connected nodes
    if len(connected_nodes) == 0:
        LOG.error("Could't find any connected MME nodes. Aborting external matching.")
        return None

    # create request headers
    headers = Headers()
    data = {'patient': json_patient(patient)} # convert into something that follows the API specs

    # this is saved to server, regardless of the results returned by the nodes
    external_match = {
        'created' : datetime.datetime.now(),
        'has_matches' : False, # it changes if a similar patient is returned by any other MME nodes
        'data' : data, # description of the patient submitted
        'results' : [],
        'errors' : [],
        'match_type' : 'external',
    }

    LOG.info("Matching patient against {} node(s)..".format(len(connected_nodes)))
    for node in connected_nodes:

        server_name = node['_id']
        node_url = node['matching_url']
        token = node['auth_token']
        request_content_type = node['accepted_content']

        headers = {'Content-Type': request_content_type,
            'Accept': 'application/vnd.ga4gh.matchmaker.v1.0+json',
            "X-Auth-Token": token,
            'Host' : host
        }
        LOG.info('sending HTTP request to server: "{}"'.format(server_name))
        # send request and get response from server
        json_response = None
        server_return = None
        try:
            server_return = requests.request(
                method = 'POST',
                url = node_url,
                headers = headers,
                data = json.dumps(data)
            )
            json_response = server_return.json()
        except Exception as json_exp:
            error = json_exp
            LOG.error('Server returned error:{}'.format(error))

            error_obj = {
                'node' : { 'id': node['_id'], 'label' : node['label'] },
                'error' : str(error)
            }
            external_match['errors'].append(error_obj)

        if json_response:
            LOG.info('server returns the following response: {}'.format(json_response))

            result_obj = {
                'node' : { 'id': node['_id'], 'label' : node['label'] },
                'patients' : []
            }
            results = json_response['results']
            if len(results):
                external_match['has_matches'] = True
                for result in results:
                    result_obj['patients'].append(result)

                external_match['results'].append(result_obj)

    return external_match


def async_match(database, response_data):
    """Creates a match object from data received from an asyc server

    Args:
        database(pymongo.database.Database)
        response_data(dict): data provided by async server in follow up request

    Returns:
        async_match(dict): a match object
    """
    # get query id you received from async server when you sent your request
    query_id = response_data['query_id'] # can't be None, checked upstream
    # collect data saved in database when you first received the async response
    async_response = database['async_responses'].find_one({'query_id':query_id})
    if not async_response:
        LOG.error("Couldn't find an asynchronous response with that id in database")
        return
    patient_id = async_response.get('query_patient_id')
    patient_obj = database['patients'].find_one({'_id':patient_id})
    node = async_response.get('node')
    if not patient_obj or not node:
        LOG.error("Query patient or external node info missing from async response.")
        return

    results = response_data.get('results') # this could be an empty list

    async_match = {
        'created' : datetime.datetime.now(),
        'has_matches' : True if len(results)>0 else False,
        'data' : {'patient' : patient_obj},
        'node' : node,
        'errors' : [], # This can be modified if I know that async server returns error
        'match_type' : 'external',
        'async' : True,
        'results' : results
    }

    return async_match
