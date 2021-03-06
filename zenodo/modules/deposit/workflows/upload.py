# -*- coding: utf-8 -*-
#
## This file is part of Zenodo.
## Copyright (C) 2012, 2013, 2014, 2015 CERN.
##
## Zenodo is free software: you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation, either version 3 of the License, or
## (at your option) any later version.
##
## Zenodo is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Zenodo. If not, see <http://www.gnu.org/licenses/>.
##
## In applying this licence, CERN does not waive the privileges and immunities
## granted to it by virtue of its status as an Intergovernmental Organization
## or submit itself to any jurisdiction.

from __future__ import absolute_import

import json
from datetime import date

from flask import render_template, url_for, request
from flask.ext.restful import fields, marshal
from flask.ext.login import current_user

from workflow import patterns as p

from invenio.base.globals import cfg
from invenio.modules.formatter import format_record
from invenio.modules.knowledge.api import get_kb_mapping
from invenio.ext.login import UserInfo
from invenio.modules.deposit.models import DepositionType, Deposition, \
    InvalidApiAction
from invenio.modules.deposit.tasks import render_form, \
    create_recid, \
    prepare_sip, \
    finalize_record_sip, \
    upload_record_sip, \
    mint_pid, \
    prefill_draft, \
    has_submission, \
    load_record, \
    merge_record, \
    process_sip_metadata, \
    process_bibdocfile
from invenio.modules.deposit.helpers import record_to_draft
from invenio.modules.deposit.tasks import merge_changes, is_sip_uploaded
from zenodo.legacy.utils.zenodoutils import create_doi, filter_empty_helper
from invenio.ext.restful import error_codes, ISODate
from zenodo.modules.deposit.forms import ZenodoForm, \
    ZenodoEditForm
from invenio.ext.sqlalchemy import db
from invenio.base.helpers import unicodifier
from invenio.modules.records.api import Record

__all__ = ['upload']

CFG_LICENSE_KB = "licenses"
CFG_LICENSE_SOURCE = "opendefinition.org"
CFG_ZENODO_USER_COLLECTION_ID = "zenodo"
CFG_ECFUNDED_USER_COLLECTION_ID = "ecfunded"


# =======
# Helpers
# =======
def file_firerole(uid, access_right, embargo_date):
    """
    Compute file firerole for a file given access_right, embargo_date.
    """
    # Generate firerole
    fft_status = []
    if access_right == 'open':
        # Access to everyone
        fft_status = []
    elif access_right == 'embargoed':
        # Access to submitter, deny everyone else until embargo date,
        # then allow all
        fft_status = [
            'allow uid "%s"' % uid,
            'deny until "%s"' % embargo_date,
            'allow any',
        ]
    elif access_right in ('closed', 'restricted',):
        # Access to submitter, deny everyone else
        fft_status = [
            'allow uid "%s"' % uid,
            'deny all',
        ]
    if fft_status:
        return "firerole: %s" % "\n".join(fft_status)
    else:
        return ""


def check_existing_pid(pid, recjson):
    """
    In Zenodo an existing pid is either 1) pre-reserved and should be minted or
    2) external and should not be minted. A user cannot enter a Zenodo pid by
    themselves.
    """
    reserved_doi = recjson.get('prereserve_doi', None)
    if reserved_doi and reserved_doi['doi'] == pid:
        return True
    return False


# =========================
# JSON processing functions
# =========================
def process_draft(draft):
    """
    Process loaded form JSON
    """
    # Filter out Zenodo and OpenAIRE communities
    draft.values['communities'] = filter(
        lambda c: c['identifier'] not in [CFG_ZENODO_USER_COLLECTION_ID,
                                          CFG_ECFUNDED_USER_COLLECTION_ID],
        draft.values.get('communities', [])
    )
    return draft


def process_recjson(deposition, recjson):
    """
    Process exported recjson (common for both new and edited records)
    """
    # ================
    # ISO format dates
    # ================
    for k in recjson.keys():
        if isinstance(recjson[k], date):
            recjson[k] = recjson[k].isoformat()

    # =======
    # Authors
    # =======
    if 'authors' in recjson and recjson['authors']:
        recjson['_first_author'] = recjson['authors'][0]
        recjson['_additional_authors'] = recjson['authors'][1:]

    # ===========
    # Communities
    # ===========
    try:
        communities = recjson.get('provisional_communities', [])

        # Extract identifier (i.e. elements are mapped from dict ->
        # string)
        recjson['provisional_communities'] = map(
            lambda x: x['identifier'],
            filter(lambda x: x.get('provisional', False), communities)
        )

        recjson['communities'] = map(
            lambda x: x['identifier'],
            filter(lambda x: not x.get('provisional', False), communities)
        )
    except TypeError:
        # Happens on re-run
        pass

    # =============================
    # Related/alternate identifiers
    # =============================
    if recjson.get('related_identifiers', []):
        related_identifiers = recjson.get('related_identifiers', [])

        recjson['related_identifiers'] = filter(
            lambda x: x.get('relation', '') != 'isAlternativeIdentifier',
            related_identifiers
        )

        recjson['alternate_identifiers'] = map(
            lambda x: {'scheme': x['scheme'], 'identifier': x['identifier']},
            filter(
                lambda x: x.get('relation', '') == 'isAlternativeIdentifier',
                related_identifiers
            )
        )

    # =================
    # License
    # =================
    if recjson['access_right'] in ["open", "embargoed"]:
        info = get_kb_mapping(CFG_LICENSE_KB, str(recjson['license']))
        if info:
            info = json.loads(info['value'])
            recjson['license'] = dict(
                identifier=recjson['license'],
                source=CFG_LICENSE_SOURCE,
                license=info['title'],
                url=info['url'],
            )
    elif 'license' in recjson:
        del recjson['license']

    # =======================
    # Journal
    # =======================
    # Set year or delete fields if no title is provided
    if recjson.get('journal.title', None):
        recjson['journal.year'] = recjson['publication_date'][:4]

    # =======================
    # References
    # =======================
    if recjson.get('references', []):
        recjson['references'] = map(
            lambda x: dict(raw_reference=x),
            recjson['references']
        )

    # =======================
    # Book/chaper/report
    # =======================
    if 'imprint.publisher' in recjson and 'imprint.place' in recjson:
        recjson['imprint.year'] = recjson['publication_date'][:4]

    if 'part_of.title' in recjson:
        mapping = [
            ('part_of.publisher', 'imprint.publisher'),
            ('part_of.place', 'imprint.place'),
            ('part_of.year', 'imprint.year'),
            ('part_of.isbn', 'isbn'),
        ]
        for new, old in mapping:
            if old in recjson:
                try:
                    recjson[new] = recjson[old]
                    del recjson[old]
                except KeyError:
                    pass

    # =================
    # Grants
    # =================
    # Remap incoming dictionary
    recjson['grants'] = map(
        lambda x: dict(
            title="%s - %s (%s)" % (x['acronym'], x['title'], x['id']),
            identifier=x['id']
        ),
        recjson.get('grants', [])
    )

    # =======================
    # Filter out empty fields
    # =======================
    filter_empty_elements(recjson)

    # ==================================
    # Map dot-keys to their dictionaries
    # ==================================
    for k in recjson.keys():
        if '.' in k:
            mainkey, subkey = k.split('.')
            if mainkey not in recjson:
                recjson[mainkey] = {}
            recjson[mainkey][subkey] = recjson.pop(k)

    return recjson


def filter_empty_elements(recjson):
    list_fields = [
        'authors', 'keywords', 'thesis_supervisors'
    ]
    for key in list_fields:
        recjson[key] = filter(
            filter_empty_helper(), recjson.get(key, [])
        )

    recjson['related_identifiers'] = filter(
        filter_empty_helper(keys=['identifier']),
        recjson.get('related_identifiers', [])
    )

    return recjson


def process_recjson_new(deposition, recjson):
    """
    Process exported recjson for a new record
    """
    process_recjson(deposition, recjson)

    # ================
    # Owner
    # ================
    # Owner of record (can edit/view the record)
    user = UserInfo(deposition.user_id)
    email = user.info.get('email', '')
    recjson['owner'] = dict(
        email=email,
        username=user.info.get('nickname', ''),
        id=deposition.user_id,
        deposition_id=deposition.id,
    )

    # ===========
    # Communities
    # ===========
    # Specific Zenodo user collection, used to curate content for
    # Zenodo
    if CFG_ZENODO_USER_COLLECTION_ID not in recjson['provisional_communities']:
        recjson['provisional_communities'].append(
            CFG_ZENODO_USER_COLLECTION_ID
        )

    # Specific Zenodo user collection for OpenAIRE (used to curate
    # FP7 funded research)
    if recjson.get('grants', []) and CFG_ECFUNDED_USER_COLLECTION_ID \
       not in recjson['provisional_communities']:
        recjson['provisional_communities'].append(
            CFG_ECFUNDED_USER_COLLECTION_ID
        )

    # ==============================
    # Files (sorting + restrictions)
    # ==============================
    fft_status = file_firerole(
        deposition.user_id,
        recjson['access_right'],
        recjson.get('embargo_date', None)
    )

    # Calculate number of leading zeros needed in the comment.
    file_commment_fmt = "%%0%dd" % len(str(len(recjson['fft'])))

    for idx, f in enumerate(recjson['fft']):
        f['restriction'] = fft_status
        # Bibdocfile does not have any concept of ordering, nor will
        # bibupload keep the order of FFT tags for the MARC field 8564.
        # Hence, this trick stores the ordering of files for a record in
        # the files comment, so files can be alphabetically sorted by their
        # comment (i.e. we add leading zeros).
        f['comment'] = file_commment_fmt % idx

    return recjson


def process_recjson_edit(deposition, recjson):
    """
    Process recjson for an edited record
    """
    process_recjson(deposition, recjson)
    # Remove all FFTs
    try:
        del recjson['fft']
    except KeyError:
        pass
    return recjson


def process_files(deposition, bibrecdocs):
    """
    Process bibrecdocs for extra files
    """
    sip = deposition.get_latest_sip(sealed=False)

    fft_status = file_firerole(
        sip.metadata['owner']['id'],
        sip.metadata['access_right'],
        sip.metadata.get('embargo_date'),
    )

    sip.metadata['fft'] = []

    for bf in bibrecdocs.list_latest_files():
        sip.metadata['fft'].append({
            'name': bf.name,
            'format': bf.format,
            'restriction': fft_status,
            'description': 'KEEP-OLD-VALUE',
            'comment': 'KEEP-OLD-VALUE',
        })


def merge(deposition, dest, a, b):
    """
    Merge changes from editing a deposition.
    """
    # A record might have been approved in communities since it was loaded,
    # thus we "manually" merge communities
    approved = set(a['communities']) & set(b['provisional_communities'])

    communities = b['communities']
    provisional = []

    for c in b['provisional_communities']:
        if c in approved:
            if c not in communities:
                communities.append(c)
        else:
            provisional.append(c)

    # Ensure that no community is in two states
    common = set(communities) & set(provisional)
    for c in common:
        provisional.pop(c)

    b['communities'] = communities
    b['provisional_communities'] = provisional

    # Append Zenodo collection
    if CFG_ZENODO_USER_COLLECTION_ID in dest['communities']:
        a['communities'].append(CFG_ZENODO_USER_COLLECTION_ID)
        b['communities'].append(CFG_ZENODO_USER_COLLECTION_ID)
    elif CFG_ZENODO_USER_COLLECTION_ID in dest['provisional_communities']:
        a['provisional_communities'].append(CFG_ZENODO_USER_COLLECTION_ID)
        b['provisional_communities'].append(CFG_ZENODO_USER_COLLECTION_ID)

    if CFG_ECFUNDED_USER_COLLECTION_ID in dest['communities']:
        a['communities'].append(CFG_ECFUNDED_USER_COLLECTION_ID)
        b['communities'].append(CFG_ECFUNDED_USER_COLLECTION_ID)
    elif CFG_ECFUNDED_USER_COLLECTION_ID in dest['provisional_communities']:
        a['provisional_communities'].append(CFG_ECFUNDED_USER_COLLECTION_ID)
        b['provisional_communities'].append(CFG_ECFUNDED_USER_COLLECTION_ID)

    b["doi"] = a["doi"]

    # Now proceed, with normal merging.
    data = merge_changes(deposition, dest, a, b)

    if 'authors' in data and data['authors']:
        data['_first_author'] = data['authors'][0]
        data['_additional_authors'] = data['authors'][1:]

    # Force ownership (owner of record (can edit/view the record))
    user = UserInfo(deposition.user_id)
    data['owner'].update(dict(
        email=user.info.get('email', ''),
        username=user.info.get('nickname', ''),
        id=deposition.user_id,
        deposition_id=deposition.id,
    ))

    return data


def transfer_ownership(deposition, user_id):
    """
    Transfer ownership of a deposition
    """
    if deposition.state != 'done':
        return False

    # Get latest uploaded SIP
    sip = deposition.get_latest_sip(sealed=True)

    if not is_sip_uploaded(sip):
        return False

    # Change user_id
    deposition.user_id = user_id
    db.session.commit()

    # Re-upload record to apply changes (e.g. file restrictions and uploader)
    deposition.reinitialize_workflow()
    deposition.run_workflow(headless=True)
    deposition.drafts['_edit'].completed = True
    deposition.run_workflow(headless=True)

    return True


# ==============
# Workflow tasks
# ==============
def run_tasks(update=False):
    """Run bibtasklet and webcoll after upload."""
    def _run_tasks(obj, dummy_eng):
        from invenio.legacy.bibsched.bibtask import task_low_level_submission

        d = Deposition(obj)
        sip = d.get_latest_sip(sealed=True)

        recid = sip.metadata['recid']

        common_args = []
        sequenceid = getattr(d.workflow_object, 'task_sequence_id', None)
        if sequenceid:
            common_args += ['-I', str(sequenceid)]

        if update:
            tasklet_name = 'bst_openaire_update_upload'
        else:
            tasklet_name = 'bst_openaire_new_upload'

        task_id = task_low_level_submission(
            'bibtasklet', 'webdeposit', '-T', tasklet_name,
            '--argument', 'recid=%s' % recid, *common_args
        )
        sip.task_ids.append(task_id)

        d.update()
    return _run_tasks


def reserved_recid():
    """
    Check for existence of a reserved recid and put in metadata so
    other tasks are not going to reserve yet another recid.
    """
    def _reserved_recid(obj, dummy_eng):
        d = Deposition(obj)
        sip = d.get_latest_sip(sealed=False)
        reserved_doi = sip.metadata.get('prereserve_doi', None)

        if reserved_doi and reserved_doi['recid']:
            sip.metadata['recid'] = reserved_doi['recid']

        d.update()
    return _reserved_recid


def api_validate_files():
    """
    Check for existence of a reserved recid and put in metadata so
    other tasks are not going to reserve yet another recid.
    """
    def _api_validate_files(obj, eng):
        if getattr(request, 'is_api_request', False):
            d = Deposition(obj)
            if len(d.files) < 1:
                d.set_render_context(dict(
                    response=dict(
                        message="Bad request",
                        status=400,
                        errors=[dict(
                            message="Minimum one file must be provided.",
                            code=error_codes['validation_error']
                        )],
                    ),
                    status=400,
                ))
                d.update()
                eng.halt("API: No files provided")
            else:
                # Mark all drafts as completed
                for draft in d.drafts.values():
                    draft.complete()
                d.update()
    return _api_validate_files


# ===============
# Deposition type
# ===============
class upload(DepositionType):
    """
    Zenodo deposition workflow
    """
    workflow = [
        p.IF_ELSE(
            has_submission,
            # Existing deposition
            [
                # Load initial record
                load_record(
                    draft_id='_edit',
                    post_process=process_draft
                ),
                # Render the form and wait until it is completed
                render_form(draft_id='_edit'),
            ],
            # New deposition
            [
                # Load pre-filled data from cache
                prefill_draft(draft_id='_default'),
                # Render the form and wait until it is completed
                render_form(draft_id='_default'),
                # Test if all files are available for API
                api_validate_files(),
            ]
        ),
        # Create the submission information package by merging data
        # from all drafts - i.e. generate the recjson.
        prepare_sip(),
        p.IF_ELSE(
            has_submission,
            [
                # Process SIP recjson
                process_sip_metadata(process_recjson_edit),
                # Merge SIP metadata into record and generate MARC
                merge_record(
                    draft_id='_edit',
                    post_process_load=process_draft,
                    process_export=process_recjson_edit,
                    merge_func=merge,
                ),
                # Set file restrictions
                process_bibdocfile(process=process_files),
            ],
            [
                # Check for reserved recids.
                reserved_recid(),
                # Reserve a new record id
                create_recid(),
                # Register DOI in internal pid store.
                mint_pid(
                    pid_field='doi',
                    pid_store_type='doi',
                    pid_creator=lambda recjson: create_doi(
                        recid=recjson['recid']
                    )['doi'],
                    existing_pid_checker=check_existing_pid,
                ),
                # Process SIP metadata
                process_sip_metadata(process_recjson_new),
            ]
        ),
        # Generate MARC based on recjson structure
        finalize_record_sip(),
        p.IF_ELSE(
            has_submission,
            [
                # Seal the SIP and write MARCXML file and call bibupload on it
                upload_record_sip(),
                # Schedule background tasks.
                run_tasks(update=True),
            ],
            [
                # Note: after upload_record_sip(), has_submission will return
                # True no matter if it's a new or editing of a deposition.
                upload_record_sip(),
                run_tasks(update=False),
            ]
        ),
    ]
    name = "Upload"
    name_plural = "Uploads"
    editable = True
    stopable = True
    enabled = True
    default = True
    api = True
    draft_definitions = {
        '_default': ZenodoForm,
        '_edit': ZenodoEditForm,
    }

    marshal_metadata_fields = dict(
        access_right=fields.String,
        communities=fields.List(fields.Raw),
        conference_acronym=fields.String,
        conference_dates=fields.String,
        conference_place=fields.String,
        conference_title=fields.String,
        conference_url=fields.String,
        conference_session=fields.String,
        conference_session_part=fields.String,
        creators=fields.Raw(default=[]),
        description=fields.String,
        doi=fields.String(default=''),
        embargo_date=ISODate,
        grants=fields.List(fields.Raw),
        image_type=fields.String(default=''),
        imprint_isbn=fields.String,
        imprint_place=fields.String,
        imprint_publisher=fields.String,
        journal_issue=fields.String,
        journal_pages=fields.String,
        journal_title=fields.String,
        journal_volume=fields.String,
        keywords=fields.Raw(default=[]),
        license=fields.String,
        notes=fields.String(default=''),
        partof_pages=fields.String,
        partof_title=fields.String,
        prereserve_doi=fields.Raw,
        publication_date=ISODate,
        publication_type=fields.String(default=''),
        references=fields.List(fields.String, default=[]),
        related_identifiers=fields.Raw(default=[]),
        thesis_supervisors=fields.Raw(default=[]),
        title=fields.String,
        upload_type=fields.String,
    )

    marshal_metadata_edit_fields = marshal_metadata_fields.copy()
    del marshal_metadata_edit_fields['prereserve_doi']
    #marshal_metadata_edit_fields.update(dict(
    #    recid=fields.Integer,
    #    version_id=UTCISODateTime,
    #))

    marshal_deposition_fields = DepositionType.marshal_deposition_fields.copy()
    del marshal_deposition_fields['drafts']

    marshal_draft_fields = DepositionType.marshal_draft_fields.copy()
    marshal_draft_fields['metadata'] = fields.Nested(
        marshal_metadata_fields, attribute='values'
    )
    del marshal_draft_fields['id']
    del marshal_draft_fields['completed']

    @classmethod
    def default_draft_id(cls, deposition):
        if deposition.has_sip() and '_edit' in deposition.drafts:
            return '_edit'
        return '_default'

    @classmethod
    def marshal_deposition(cls, deposition):
        """
        Generate a JSON representation for REST API of a Deposition
        """
        # Get draft
        if deposition.has_sip() and '_edit' in deposition.drafts:
            draft = deposition.get_draft('_edit')
            metadata_fields = cls.marshal_metadata_edit_fields
        elif deposition.has_sip():
            # FIXME: Not based on latest available data in record.
            sip = deposition.get_latest_sip(sealed=True)
            draft = record_to_draft(
                Record.create(sip.package, master_format='marc'),
                post_process=process_draft
            )
            metadata_fields = cls.marshal_metadata_edit_fields
        else:
            draft = deposition.get_or_create_draft('_default')
            metadata_fields = cls.marshal_metadata_fields

        # Fix known differences in marshalling
        draft.values = filter_empty_elements(draft.values)
        if 'grants' not in draft.values:
            draft.values['grants'] = []

        # Set disabled values to None in output
        for field, flags in draft.flags.items():
            if 'disabled' in flags and field in draft.values:
                del draft.values[field]

        # Marshal deposition
        obj = marshal(deposition, cls.marshal_deposition_fields)
        # Marshal the metadata attribute
        obj['metadata'] = marshal(unicodifier(draft.values), metadata_fields)

        # Add record and DOI information from latest SIP
        for sip in deposition.sips:
            if sip.is_sealed():
                recjson = sip.metadata
                if recjson.get('recid'):
                    obj['record_id'] = fields.Integer().format(
                        recjson.get('recid')
                    )
                    obj['record_url'] = fields.String().format(url_for(
                        'record.metadata',
                        recid=recjson.get('recid'),
                        _external=True
                    ))
                if recjson.get('doi') and \
                   recjson.get('doi').startswith(cfg['CFG_DATACITE_DOI_PREFIX']
                                                +"/"):
                    obj['doi'] = fields.String().format(recjson.get('doi'))
                    obj['doi_url'] = fields.String().format(
                        "http://dx.doi.org/%s" % obj['doi']
                    )
                break

        return obj

    @classmethod
    def marshal_draft(cls, obj):
        """
        Generate a JSON representation for REST API of a DepositionDraft
        """
        return marshal(obj, cls.marshal_draft_fields)

    @classmethod
    def api_action(cls, deposition, action_id):
        if action_id == 'publish':
            return deposition.run_workflow(headless=True)
        elif action_id == 'edit':
            # Trick: Works in combination with load_record task to provide
            # proper response codes to API clients.
            if deposition.state == 'done' or deposition.drafts:
                deposition.reinitialize_workflow()
            return deposition.run_workflow(headless=True)
        elif action_id == 'discard':
            deposition.stop_workflow()
            deposition.save()
            return deposition.marshal(), 201
        raise InvalidApiAction(action_id)

    @classmethod
    def api_metadata_schema(cls, draft_id):
        schema = super(upload, cls).api_metadata_schema(draft_id)
        if schema and draft_id == '_edit':
            if 'recid' in schema['schema']:
                del schema['schema']['recid']
            if 'modification_date' in schema['schema']:
                del schema['schema']['modification_date']
        return schema

    @classmethod
    def render_completed(cls, d):
        """
        Render page when deposition was successfully completed
        """
        ctx = dict(
            deposition=d,
            deposition_type=(
                None if d.type.is_default() else d.type.get_identifier()
            ),
            uuid=d.id,
            my_depositions=Deposition.get_depositions(
                current_user, type=d.type
            ),
            sip=d.get_latest_sip(),
            format_record=format_record,
        )

        return render_template('deposit/completed.html', **ctx)
