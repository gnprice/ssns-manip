#!/usr/bin/env python3

"""
Copyright (c) 2012, CCL Forensics
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of the CCL Forensics nor the
      names of its contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL CCL FORENSICS BE LIABLE FOR ANY
DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import sys
import struct
import io
from os import SEEK_CUR, SEEK_END, SEEK_SET
import traceback
import xml.etree.ElementTree as etree # For reporting. Not used during parsing.
import xml.dom.minidom as minidom # Again, only for reporting (prettyprint)


__version__ = "0.10.0"
__description__ = "Parses the Chrome Session/Tab restore (SNSS) files"
__contact__ = "Alex Caithness"

FILE_SIGNATURE = b"SNSS"
FILE_VERSION = 1

FILE_TYPE_TABS = 1
FILE_TYPE_SESSION = 2

# https://chromium.googlesource.com/chromium/src.git/+/refs/tags/89.0.4389.90/components/sessions/core/command_storage_backend.cc
kInitialStateMarkerCommandId = 255

PAGE_TRANSITION_TYPES = {
                         0 : "Link followed",
                         1 : "URL Typed",
                         2 : "Followed UI suggestion (eg. Bookmarks, Destinations Page)",
                         3 : "Automatic sub-frame navigation",
                         4 : "Manual sub-frame navigation",
                         5 : "Selected from omni-box suggestion",
                         6 : "Start page",
                         7 : "Submitted a form",
                         8 : "Reloaded page",
                         9 : "Generated as a result of keyword in omni-box",
                         10: "Generated by a keyword in moni-box"
                         }
PAGE_TRANSITION_TYPE_MASK = 0xFF

PAGE_TRANSITION_QUALIFIERS = {
                              0x01000000 : "Back-forward list",
                              0x02000000 : "To home page",
                              0x10000000 : "Navigation chain start",
                              0x20000000 : "Last transition in redirect chain",
                              0x40000000 : "Client-side redirect", # eg script/ meta-refresh
                              0x80000000 : "Server-side redirect", # eg redirect in http header
                              }

PAGE_TRANSITION_QUALIFIER_MASK = 0xFFFFFF00

SKIP_ERRORS = False
USE_EXPERIMENTAL_FEATURES = True # if you are getting errors or strange output try setting to False

def log(*args):
    print(*args, file=sys.stderr)

class SsnsError(Exception):
    pass

class WebHistoryItem:
    '''
    A "page state", as serialized by SerializedNavigationDriver.

    This can apparently be quite different between different flavors
    of Chromium-based browser.  The implementation that appears to be
    involved for desktop browsers delegates to Blink:
      https://chromium.googlesource.com/chromium/src.git/+/refs/tags/89.0.4389.90/components/sessions/content/content_serialized_navigation_driver.cc#64
    where the relevant code appears to be ReadPageState here:
      https://chromium.googlesource.com/chromium/src.git/+/refs/tags/89.0.4389.90/third_party/blink/common/page_state/page_state_serialization.cc#889

    Then there's an internal format-version field which seems quite
    frequently incremented, like several times a year, with changes
    that are incompatible for old readers.  The current (as of
    Chrome 89) implementation refuses to even try to parse anything
    older than version 11, and at version 26 it changed to a
    completely new thing again.
    '''

    def __init__(self, url_string, original_url, target, parent, title, alt_title, last_visited_time, 
                 scroll_offset_x, scroll_offset_y, is_target_item, visit_count, referrer, document_state,
                 page_scale_factor, item_sequence_number, document_sequence_number, state_obj, form_data,
                 http_content_type, sub_items):
        
        # Excuse the layout, just assigning all incoming variables
        (self.url_string, 
         self.original_url, 
         self.target, 
         self.parent, 
         self.title, 
         self.alt_title, 
         self.last_visited_time, 
         self.scroll_offset_x, 
         self.scroll_offset_y, 
         self.is_target_item, 
         self.visit_count, 
         self.referrer, 
         self.document_state,
         self.page_scale_factor, 
         self.item_sequence_number, 
         self.document_sequence_number, 
         self.state_obj, 
         self.form_data,
         self.http_content_type, 
         self.sub_items) = (url_string, 
                            original_url, 
                            target, 
                            parent, 
                            title, 
                            alt_title, 
                            last_visited_time, 
                            scroll_offset_x, 
                            scroll_offset_y, 
                            is_target_item, 
                            visit_count, 
                            referrer, 
                            document_state,
                            page_scale_factor, 
                            item_sequence_number, 
                            document_sequence_number, 
                            state_obj, 
                            form_data,
                            http_content_type, 
                            sub_items)

    def parse_document_state_text(self):
        res = []
        if self.document_state:
            for i in range(0, len(self.document_state), 3):
                state_slice = self.document_state[i:i+3]
                padcount = (3 - (len(state_slice) % 3))
                state_slice += [""]*padcount
                res.append("Name: \"{0}\"; Type: \"{1}\"; Value: \"{2}\"".format(state_slice[0], state_slice[1], state_slice[2]))
            
        return res

    def parse_document_state(self):
        res = []
        if self.document_state:
            for i in range(0, len(self.document_state), 3):
                state_slice = self.document_state[i:i+3]
                padcount = (3 - (len(state_slice) % 3))
                state_slice += [""]*padcount
                res.append(tuple(state_slice))
            
        return res

    def parse_form_data(self): # Experimental
        for form in self.form_data or []:
            if not isinstance(form, bytes):
                continue # Currently only understand the "blob" field

            awaiting_input = 0x00
            in_boundary = 0x01
            in_data = 0x02

            state = awaiting_input
            name = None
            value = []
            
            form_data_lines = form.decode("utf-8").splitlines()
            # Find first non-blank line, decide how to continue
            is_webkit_parse = False
            first_line = ""
            for line in form_data_lines:
                if line.strip() == "":
                    continue
                else:
                    first_line = line
                    break
            if not first_line.startswith("------WebKitFormBoundary"):
                # just yield as raw and leave
                yield "Form Data", form.decode("utf-8")
                break

            

            for line in form.decode("utf-8").splitlines():
                
                if line.strip() == "":
                    continue
                
                if state == awaiting_input:
                    if  not line.startswith("------WebKitFormBoundary"):
                        raise Exception("Unexpected input while parsing Form Data")
                    else:
                        state = in_boundary
                        name = None
                        value = []

                elif state == in_boundary:
                    if not line.startswith("Content-Disposition"):
                        raise Exception("Unexpected input while parsing Form Data")
                    else:
                        fields = [f.strip() for f in line.split(";")]
                        content_disposition = fields[0].split(":")[1].strip()
                        if content_disposition != "form-data":
                            raise Exception("Unexpected content-dispostion while parsing Form Data ({0})".format(content_disposition))
                        for field in fields[1:]:
                            k,v = field.split("=", 1)
                            if k == "name":
                                name = v.strip("\"")
                        if name == None:
                            raise Exception("Form field name still unknown after parsing form-data Content-Disposition")
                        state = in_data

                elif state == in_data:
                    if line.startswith("------WebKitFormBoundary"):
                        yield name, "\n".join(value)
                        state = in_boundary
                        name = None
                        value = []
                    else:
                        value.append(line)
                
                else:
                    raise Exception("Invalid state ({0})".format(state))
         
    @classmethod
    def from_bytes(cls, data):
        # Pop the data into a BytesIO for convenience
        f = io.BytesIO(data)
        return cls.from_stream(f)
    
    @classmethod
    def from_stream(cls, f):
        # Details of the encoding can be found in chrome source, webkit/glue/glue_serialize.cc

        # Encountered an edge case (or possibly mal-formed data) where in the
        # subitems (see below) we end being told that we have more than we
        # actually have (or the data is truncated). At which point we'll be 
        # given an empty string here. We need to back out and return None
        # at which point.
        try:
            version, = struct.unpack("<i", f.read(4))
        except struct.error:
            # empty stream, abandon all hope
            return None

        # If the version is -1 all we have is a url string and we can
        # leave early with just that.
        if version == -1:
            url = read_str_8(f)
            return cls(url, None, None, None, None, None, None, None, None, 
                   None, None, None, None, None, None, None,
                   None, None, None, None)

        # At version 14, the format changed in ways this code doesn't currently support.
        if version >= 14:
            return None

        # Based on the version, the strings may be encoded differently.
        # (See: webkit/glue/glue_serialize.cc; WriteString in Chrome source).
        string_length_is_bytes = version == 1 or version >= 3

        url = read_str_16(f, string_length_is_bytes)
        original_url = read_str_16(f, string_length_is_bytes)
        target = read_str_16(f, string_length_is_bytes)
        parent = read_str_16(f, string_length_is_bytes)
        title = read_str_16(f, string_length_is_bytes)
        alt_title = read_str_16(f, string_length_is_bytes)
        
        # The timestamp is written as a double which has no native support in
        # pickles. Because of this, the calling code actually uses "WriteData"
        # which means that it will be serialized with an int32 first, giving its
        # length (which we can skip)
        f.seek(4, SEEK_CUR) # skip the int
        timestamp, = struct.unpack("<d", f.read(8)) # huh?
        x_scroll_offset, = struct.unpack("<i", f.read(4)) 
        y_scroll_offset, = struct.unpack("<i", f.read(4)) 
        is_target_item = struct.unpack("<i", f.read(4)) [0] > 0
        visit_count, = struct.unpack("<i", f.read(4)) 
        referrer = read_str_16(f, string_length_is_bytes)

        # document state is a list of strings. First get the count
        doc_state_count, = struct.unpack("<i", f.read(4)) 
        doc_state = []
        # Then get the strings
        for i in range(doc_state_count):
            doc_state.append(read_str_16(f, string_length_is_bytes))

        # There are a few version dependant fields next
        page_scale_factor = None
        item_sequence_number = None
        document_sequence_number = None
        if version >= 11:
            # Stored as a double again, so skip 4 bytes first
            f.seek(4, SEEK_CUR)
            page_scale_factor, = struct.unpack("<d", f.read(8))
        if version >= 9:
            item_sequence_number, = struct.unpack("<q", f.read(8))
        if version >= 6:
            document_sequence_number, = struct.unpack("<q", f.read(8))
        
        # The state object actually begins with a bool telling us whether it's there:
        has_state_object = struct.unpack("<i", f.read(4))[0] > 0
        state_object = read_str_16(f, string_length_is_bytes) if has_state_object else None

        # Form data - 
        # Haven't tested this as it never populated during testing. The form
        # (no pun intended) in which I output the data is a little messy for 
        # that reason. Easy to refactor at a later date though.
        # as above, we start with a bool letting us know if this is present
        has_form_data = struct.unpack("<i", f.read(4))[0] > 0
        form_data = []
        if has_form_data:
            # then we get the number of records
            form_data_count, = struct.unpack("<i", f.read(4))
            for i in range(form_data_count):
                # Then depending on the type value (derrived from an Enum in 
                # webkit WebHTTPBody.h) we do stuff to the data accordingly.
                # (Annoyingly I haven't had a chance to test this as I can't 
                # force a test case where this actually populates!)
                record_type, =  struct.unpack("<i", f.read(4))
                if record_type == 0:
                    # Blob of data
                    b_length, =  struct.unpack("<i", f.read(4))
                    form_data.append(f.read(b_length))
                    if b_length % 4 == 0:
                        align_skip_count = 0
                    else:
                        align_skip_count = 4 - (b_length % 4)
                    f.read(align_skip_count)
                elif record_type == 1 or record_type == 2:
                    file_path = read_str_16(f, string_length_is_bytes)
                    file_start, = struct.unpack("<q", f.read(8))
                    file_length, = struct.unpack("<q", f.read(8))
                    # Timestamp, double, skip length value
                    f.seek(4, SEEK_CUR)
                    mod_time, = struct.unpack("<d", f.read(8))
                    form_data.append((file_path, file_start, file_length, mod_time))
                else:
                    # Just a url
                    form_data.append(read_str_16(f, string_length_is_bytes))

            form_body_identifier, = struct.unpack("<q", f.read(8))
            form_has_passwords = struct.unpack("<i", f.read(4))[0] > 0

        content_type = read_str_16(f, string_length_is_bytes)
        # referrer happens again (for backwards compatibility apparently) we've
        # already got it so we can just throw it away
        # UPDATE: found a case where this isn't present, so much for backwards
        #         compatibility (not that we ever actually cared about it. So
        #         anyway, we need to catch that case here. Of course, if this
        #         is missing, so are the sub items so we need to let that bit
        #         of the code not to bother as well
        this_state_is_truncated = False
        try:
            read_str_16(f, string_length_is_bytes)
        except struct.error:
            this_state_is_truncated = True
        
        # Finally there can be subitems (if we're not truncated)...
        sub_items = []
        if not this_state_is_truncated:
            sub_item_count, = struct.unpack("<i", f.read(4))
            for i in range(sub_item_count):
                sub = WebHistoryItem.from_stream(f)
                if sub:
                    sub_items.append(sub)
                else:
                    # We got None back from from_stream() which means something 
                    # went wrong. Give up and take what we've got.
                    break


        return cls(url, original_url, target, parent, title, alt_title, timestamp, x_scroll_offset, y_scroll_offset, 
                   is_target_item, visit_count, referrer, doc_state, page_scale_factor, item_sequence_number, document_sequence_number,
                   state_object, form_data, content_type, sub_items)


class SessionCommand:

    def __init__(self, command_type_id, tab_id, index, url, title, web_history_item, transition_type, has_post_data,
                 referrer_url, referrer_policy, request_url, is_overriding_user_agent):
        self.command_type_id = command_type_id
        self.tab_id = tab_id
        self.index = index
        self.url = url
        self.title = title
        self.web_history_item = web_history_item
        self.transition_type = transition_type
        self.has_post_data = has_post_data
        self.referrer_policy = referrer_policy
        self.request_url = request_url
        self.is_overriding_user_agent = is_overriding_user_agent

    def __repr__(self):
        return "Tab ID {0}; Index: {1}; URL: {2}; Title: {3};".format(self.tab_id, self.index, self.url, self.title)

    def __str__(self):
        return self.__repr__()

    def get_transition_type(self):
        if self.transition_type==None:
            return "N/A"
        elif self.transition_type & PAGE_TRANSITION_TYPE_MASK in PAGE_TRANSITION_TYPES:
            return PAGE_TRANSITION_TYPES[self.transition_type & PAGE_TRANSITION_TYPE_MASK]
        else:
            return "Unknown Transition"

    def get_transition_qualifiers(self):
        res = []
        if not self.transition_type:
            return res
        
        for q in PAGE_TRANSITION_QUALIFIERS:
            if self.transition_type & PAGE_TRANSITION_QUALIFIER_MASK == q:
                res.append(PAGE_TRANSITION_QUALIFIERS[q])
        return res

def read_str_8(f):
    # The strings are written into a Pickle so they take the form:
    # - int32 string length (character count)
    # - string buffer
    # The string buffer will be the string length multiplied by the character width in length
    # (so in this case, with 8-bit encoding it will be string length x 1)
    # Pickles write everything to be uint32 aligned, so if the string buffer is not a multiple
    # of 4 in length we need to skip over a few extra bytes at the end to make up the difference

    char_count, = struct.unpack("<i", f.read(4))
    string_buffer_length = char_count
    if string_buffer_length % 4 == 0:
        align_skip_count = 0
    else:
        align_skip_count = 4 - (string_buffer_length % 4)
    string = f.read(string_buffer_length).decode("ascii")
    f.seek(align_skip_count, SEEK_CUR)
    return string

def read_str_16(f, length_is_bytes=False):
    # The strings are written into a Pickle so they take the form:
    # - int32 string length (character count)
    # - string buffer
    # The string buffer will be the string length multiplied by the character width in length
    # (so in this case, with 16-bit encoding it will be string length x 2, unless 'length_is_bytes'
    # is True.
    # Pickles write everything to be uint32 aligned, so if the string buffer is not a multiple
    # of 4 in length we need to skip over a few extra bytes at the end to make up the difference

    char_count, = struct.unpack("<i", f.read(4))
    
    if char_count == -1:
        return None
    
    if length_is_bytes:
        string_buffer_length = char_count
    else:
        string_buffer_length = char_count * 2
    if string_buffer_length % 4 == 0:
        align_skip_count = 0
    else:
        align_skip_count = 4 - (string_buffer_length % 4)
    string = f.read(string_buffer_length).decode("utf_16_le")
    f.seek(align_skip_count, SEEK_CUR)
    return string

def read_command(f):
    start_offset = f.tell()

    # Try to get the size of the record.
    size_bytes = f.read(2)
    if len(size_bytes) < 2:
        return None # we've hit the end of the file

    # Get the command
    command_size, = struct.unpack("<H", size_bytes)
    command_bytes = f.read(command_size)

    # Check that it's long enough
    if len(command_bytes) < command_size:
        raise SsnsError("Error: Command bytes is less than the stated command size. We have hit the end of the stream prematurely")
        #return None # we've hit the end of the file prematurely

    # Put bytes into a BytesIO to make life easier
    command_buffer = io.BytesIO(command_bytes)

    # Get the command type id
    command_id = command_buffer.read(1)[0]

    # An UpdateTabNavigation command is stored with command type 1
    # in Tabs files, and command type 6 in Session files.
    # (And Session files have no command type 1.)
    # See kCommandUpdateTabNavigation in tab_restore_service_impl.cc
    # and session_service_commands.cc respectively.
    if command_id in (1,6):
        command = read_navigation_entry(command_buffer, command_id)
        print_navigation(start_offset, command)
        return command
    else:
        print_command(start_offset, command_id, command_buffer)
        return SessionCommand(command_id, None, None, None, None, None, None, None, 
                          None, None, None, None)


def print_navigation(start_offset, command):
    print(f"{start_offset:08x}: C{6:<3} T{command.tab_id:04x} navindex{command.index:x} {command.url}")


def print_command(start_offset, command_id, command_buffer):
    # For command types, see:
    #   https://chromium.googlesource.com/chromium/src.git/+/refs/tags/94.0.4606.61/components/sessions/core/session_service_commands.cc
    # For the format for each type, see CreateTabsAndWindows there.

    def read_words(n):
        return struct.unpack(f'<{n}i', command_buffer.read(n * 4))

    if command_id == 0: # kCommandSetTabWindow
        window_id, tab_id = read_words(2)
        description = f"W{window_id:02x} T{tab_id:04x}"

    # obsolete: elif command_id == 1: # kCommandSetWindowBounds

    elif command_id == 2: # kCommandSetTabIndexInWindow
        tab_id, index = read_words(2)
        description = f"T{tab_id:04x} {index}"

    # obsolete: elif command_id == 5: # kCommandTabNavigationPathPrunedFromBack
    # handled above: elif command_id == 6: # kCommandUpdateTabNavigation

    elif command_id == 7: # kCommandSetSelectedNavigationIndex
        tab_id, index = read_words(2)
        description = f"T{tab_id:04x} {index}"

    elif command_id == 8: # kCommandSetSelectedTabInIndex
        window_id, index = read_words(2)
        description = f"W{window_id:02x} {index}"

    # elif command_id == 9: # kCommandSetWindowType
    # # obsolete: elif command_id == 10: # kCommandSetWindowBounds2
    # # obsolete: elif command_id == 11: # kCommandTabNavigationPathPrunedFromFront
    # elif command_id == 12: # kCommandSetPinnedState
    # elif command_id == 13: # kCommandSetExtensionAppID
    # elif command_id == 14: # kCommandSetWindowBounds3
    # elif command_id == 15: # kCommandSetWindowAppName
    # elif command_id == 16: # kCommandTabClosed
    # elif command_id == 17: # kCommandWindowClosed
    # # obsolete: elif command_id == 18: # kCommandSetTabUserAgentOverride
    # elif command_id == 19: # kCommandSessionStorageAssociated
    # elif command_id == 20: # kCommandSetActiveWindow
    # elif command_id == 21: # kCommandLastActiveTime
    # # obsolete: elif command_id == 22: # kCommandSetWindowWorkspace
    # elif command_id == 23: # kCommandSetWindowWorkspace2
    # elif command_id == 24: # kCommandTabNavigationPathPruned
    # elif command_id == 25: # kCommandSetTabGroup
    # # obsolete: elif command_id == 26: # kCommandSetTabGroupMetadata
    # elif command_id == 27: # kCommandSetTabGroupMetadata2
    # elif command_id == 28: # kCommandSetTabGuid
    # elif command_id == 29: # kCommandSetTabUserAgentOverride2
    # elif command_id == 30: # kCommandSetTabData
    # elif command_id == 31: # kCommandSetWindowUserTitle
    # elif command_id == 32: # kCommandSetWindowVisibleOnAllWorkspaces

    # TODO nice formatting for more command types, above
    else:
        description = command_buffer.read().hex(' ', 4)

    print(f"{start_offset:08x}: C{command_id:<3} {description}")


def read_navigation_entry(command_buffer, command_id):
    '''
    Read a tab ID and SerializedNavigationEntry.

    Corresponds to RestoreUpdateTabNavigationCommand:
      https://chromium.googlesource.com/chromium/src.git/+/refs/tags/89.0.4389.90/components/sessions/core/base_session_service_commands.cc#152

    Everything after tab_id corresponds to SerializedNavigationEntry::ReadFromPickle:
      https://chromium.googlesource.com/chromium/src.git/+/refs/tags/89.0.4389.90/components/sessions/core/serialized_navigation_entry.cc#178
    '''

    # Get the pickle length value (we won't actually use it, just carry on through the buffer)
    pickle_length, = struct.unpack("<i", command_buffer.read(4))

    # We're going to assume that the data is complete and correct
    tab_id, index = struct.unpack("<2i", command_buffer.read(8))
    url = read_str_8(command_buffer)
    title = read_str_16(command_buffer)
    state_length, = struct.unpack("<i", command_buffer.read(4))
    state_blob = command_buffer.read(state_length)
    if state_length % 4 == 0:
        align_skip_count = 0
    else:
        align_skip_count = 4 - (state_length % 4)
    command_buffer.seek(align_skip_count, SEEK_CUR)
    transition_type, has_post_data = struct.unpack("<2i", command_buffer.read(8))
    referrer_url = read_str_8(command_buffer)
    
    # It appears that some (older?) versions of the file format didn't have these last two or three fields
    # deal with that here
    if command_buffer.tell() >= len(command_buffer.getvalue()):
        referrer_policy = 0
    else:
        # NB this is apparently wrong and ignored -- correct one comes later
        referrer_policy, = struct.unpack("<i", command_buffer.read(4))

    if command_buffer.tell() >= len(command_buffer.getvalue()):
        request_url = ""
        is_overriding_user_agent = 0
    else:
        request_url = read_str_8(command_buffer)
        is_overriding_user_agent, = struct.unpack("<i", command_buffer.read(4))

    # Then:
    #   8 bytes of timestamp
    #   UTF-16 string search_terms (ignored)
    #   4 bytes HTTP status code
    #   4 bytes referrer policy -- correct one this time
    #      If missing, page state gets referrer stripped out
    #   4 bytes extended info map size
    #   N times 2 times byte-string, for key-value pairs for extended info map
    #   8 bytes task ID
    #   8 bytes parent task ID
    #   8 bytes root task ID
    #   4 bytes num children task IDs
    #   N times 8 bytes for children task IDs
    #   (that's it, as of Chrome 89)

    # Parse state
    state = None
    if state_length > 4:
        state = WebHistoryItem.from_bytes(state_blob[4:]) # first 32bits is the internal pickle size. We dont' need it.
    if state is None:
        state = WebHistoryItem(None, None, None, None, None, None, None, None, None, 
                   None, None, None, None, None, None, None,
                   None, None, None, None)

    return SessionCommand(command_id, tab_id, index, url, title, state, transition_type, has_post_data > 0, 
                          referrer_url, referrer_policy, request_url, is_overriding_user_agent > 0)


def read_header(f):
    # Check header
    sig = f.read(len(FILE_SIGNATURE))
    if sig != FILE_SIGNATURE:
        raise ValueError("File signature is not SNSS")
    
    ver, = struct.unpack("<i", f.read(4))
    if ver not in (1, 3):
        raise ValueError("File version is not 1 or 3")

    return ver


""" f should be a file like object """
def load_iter(f, file_type):
    _ = read_header(f)

    while True: 
        record_start_offset = f.tell()
        try:
            command = read_command(f)
        except BrokenPipeError:
            raise
        except (struct.error, IOError, SsnsError) as e:
            log("Error reading record begining at data offset {0}.".format(record_start_offset))
            log("Error caused by: {0}.".format(e))
            log("Traceback follows for debugging:")
            log()
            log("---------------EXCEPTION BEGINS---------------")
            traceback.print_exc(limit=None, file=sys.stderr)
            log("----------------EXCEPTION ENDS----------------")
            log()
            
            if SKIP_ERRORS:
                continue
            else:
                log("NB: No further records will be read.")
                command = None
        if command:
            yield command
        else:
            break

def load(f, file_type):
    return [c for c in load_iter(f, file_type)]


# --- command line and reporting stuff ---

html_style_string = """
body{ font-family:Trebuchet,Arial,Sans-serif } 
table.command_table { width: 100%; border: 1px #000000 solid; padding: 5px; border-collapse: collapse}
tr.command_row { background-color:#DDDDDD  }
tr.command_row:hover { background-color:#EEEEEE  }
td.command_attr { font-weight: bold; font-size: 0.75em; border: 1px #000000 dotted;  }
td.command_value { font-weight: normal; font-size: 0.75em; border: 1px #000000 dotted; word-wrap: break-word; }

"""

def build_command_table(command, parent_element):
    command_table = etree.SubElement(parent_element, "table", {"class":"command_table"})
    etree.SubElement(command_table, "col", {"width":"20%"} )
    etree.SubElement(command_table, "col", {"width":"80%"} )

    # --ROW--
    command_row = etree.SubElement(command_table, "tr", {"class":"command_row"})
    
    # Header
    id_head_td = etree.SubElement(command_row, "td", {"class":"command_attr"})
    id_head_td.text = "Tab ID"
    id_data_td = etree.SubElement(command_row, "td", {"class":"command_value"})
    id_data_td.text = str(command.tab_id)

    # --ROW--
    command_row = etree.SubElement(command_table, "tr", {"class":"command_row"})

    # Index
    id_head_td = etree.SubElement(command_row, "td", {"class":"command_attr"})
    id_head_td.text = "Index"
    id_data_td = etree.SubElement(command_row, "td", {"class":"command_value"})
    id_data_td.text = str(command.index)

    # --ROW--
    command_row = etree.SubElement(command_table, "tr", {"class":"command_row"})

    # URL
    url_head_td = etree.SubElement(command_row, "td", {"class":"command_attr"})
    url_head_td.text = "URL"
    url_data_td = etree.SubElement(command_row, "td", {"class":"command_value"})
    url_data_td.text = command.url

    # --ROW--
    command_row = etree.SubElement(command_table, "tr", {"class":"command_row"})

    # Title
    title_head_td = etree.SubElement(command_row, "td", {"class":"command_attr"})
    title_head_td.text = "Title"
    title_data_td = etree.SubElement(command_row, "td", {"class":"command_value"})
    title_data_td.text = command.title

    # --ROW--
    command_row = etree.SubElement(command_table, "tr", {"class":"command_row"})

    # Request URL
    request_url_head_td = etree.SubElement(command_row, "td", {"class":"command_attr"})
    request_url_head_td.text = "Request URL"
    request_url_data_td = etree.SubElement(command_row, "td", {"class":"command_value"})
    request_url_data_td.text = command.request_url

    # --ROW--
    command_row = etree.SubElement(command_table, "tr", {"class":"command_row"})
    
    # Referrer URL
    referrer_head_td = etree.SubElement(command_row, "td", {"class":"command_attr"})
    referrer_head_td.text = "Referrer URL"
    referrer_data_td = etree.SubElement(command_row, "td", {"class":"command_value"})
    referrer_data_td.text = command.web_history_item.referrer

    # --ROW--
    command_row = etree.SubElement(command_table, "tr", {"class":"command_row"})
    
    # Transition Type
    trans_head_td = etree.SubElement(command_row, "td", {"class":"command_attr"})
    trans_head_td.text = "Transition Type"
    trans_data_td = etree.SubElement(command_row, "td", {"class":"command_value"})
    trans_data_td.text = "{0} ({1})".format(command.get_transition_type() , ", ".join(command.get_transition_qualifiers()))

    # --ROW--
    command_row = etree.SubElement(command_table, "tr", {"class":"command_row"})
    
    # Document State
    doc_state_head_td = etree.SubElement(command_row, "td", {"class":"command_attr"})
    doc_state_head_td.text = "Document States (Including document sub-items)"
    doc_state_data_td = etree.SubElement(command_row, "td", {"class":"command_value"})
    
    def recurs_doc_state(whi, node):
        for item in whi.parse_document_state_text():
            doc_state_data_p = etree.SubElement(node, "p", {"class":"no-space-after"})
            doc_state_data_p.text = item
            
        for sub in whi.sub_items or []:
            recurs_doc_state(sub, node)

    recurs_doc_state(command.web_history_item, doc_state_data_td)

    # --ROW--
    command_row = etree.SubElement(command_table, "tr", {"class":"command_row"})

    # FORM DATA (EXPERIMENTAL)
    if USE_EXPERIMENTAL_FEATURES:
        form_data_head_td = etree.SubElement(command_row, "td", {"class":"command_attr"})
        form_data_head_td.text = "Submitted Form Data (EXPERIMENTAL)"
        form_data_data_td = etree.SubElement(command_row, "td", {"class":"command_value"})

        def recurs_form_data(whi, node):
            for item in whi.parse_form_data():
                form_data_data_p = etree.SubElement(node, "p", {"class":"no-space-after"})
                form_data_data_p.text = "Name: \"{0}\"; Value: \"{1}\"".format(*item)
            
            for sub in whi.sub_items or []:
                recurs_form_data(sub, node)
    
        recurs_form_data(command.web_history_item, form_data_data_td)


def write_report(commands, out_path):
    document_root = etree.Element("html", )
    document_head = etree.SubElement(document_root, "head")
    document_style = etree.SubElement(document_head, "style", {"type":"text/css"})
    document_style.text = html_style_string
    etree.SubElement(document_head, "meta", {"http-eqiv":"Content-Type", "content":"text/html", "charset":"utf-8"})

    body = etree.SubElement(document_root, "body")

    for c in sorted(commands, key=lambda rec: (rec.tab_id or -1, rec.index or -1)):
        if c.command_type_id in (1,6):
            build_command_table(c, body)
            etree.SubElement(body, "br")

    # Write output (using minidom for prettification)
    out = open(out_path, "wt", encoding="utf-8")
    #out.write(minidom.parseString(etree.tostring(document_root, encoding="utf-8").decode()).toprettyxml())
    out.write(etree.tostring(document_root, encoding="utf-8").decode())
    out.close()


def main():
    if len(sys.argv) < 3:
        log("Usage: <Current/Last Session/Tabs> <output.html>")
        sys.exit() 
    
    log("Processing begins...")
    # load infile
    with open(sys.argv[1], "rb") as f:
        commands = load(f, FILE_TYPE_TABS)

    with open(sys.argv[1], "rb") as f:
        file_format_version = read_header(f)

    supports_marker = (file_format_version in (3, 4))
    if supports_marker:
        has_marker = kInitialStateMarkerCommandId in (c.command_type_id for c in commands)
        if not has_marker:
            # For background, see comment on CommandStorageBackend:
            #   https://chromium.googlesource.com/chromium/src.git/+/refs/tags/89.0.4389.90/components/sessions/core/command_storage_backend.h
            log("NO MARKER.  File was incompletely written.")

    write_report(commands, sys.argv[2])

    log("Processing finished.")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
