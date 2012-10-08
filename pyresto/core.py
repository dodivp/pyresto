# coding: utf-8

"""
pyresto.core
~~~~~~~~~~~~

This module contains all core pyresto classes such as Error, Model and relation
classes.

"""

import collections
import logging
try:
    import json
except ImportError:
    import simplejson as json
import re
import urlparse

import requests

from abc import ABCMeta, abstractproperty, abstractmethod
from urllib import quote


__all__ = ('PyrestoException',
           'PyrestoInvalidOperationException',
           'PyrestoServerResponseException',
           'PyrestoInvalidRestMethodException',
           'PyrestoInvalidAuthTypeException',
           'Model', 'Many', 'Foreign')

ALLOWED_HTTP_METHODS = frozenset(('GET', 'POST', 'PUT', 'DELETE', 'PATCH'))


def assert_class_instance(class_method):
    def asserted(cls, instance, *args, **kwargs):
        assert isinstance(instance, cls)
        return class_method(cls, instance, *args, **kwargs)

    return asserted


def normalize_auth(class_method):
    def normalized(cls, instance, *args, **kwargs):
        auth = kwargs.get('auth')
        if auth is None:
            auth = cls._auth or instance._auth
        kwargs['auth'] = auth
        return class_method(cls, instance, *args, **kwargs)

    return normalized


class PyrestoException(Exception):
    """Base error class for pyresto."""


class PyrestoInvalidOperationException(PyrestoException):
    """Invalid operation error class for Pyresto."""


class PyrestoServerResponseException(PyrestoException):
    """Server response error class for pyresto."""


class PyrestoInvalidRestMethodException(PyrestoException, ValueError):
    """A valid HTTP method is required to make a request."""


class PyrestoInvalidAuthTypeException(PyrestoException, ValueError):
    """
    Error class for exceptions thrown when an invalid auth type is used with
    the global authentication function generated by :func:`enable_auth`
    """


class ModelBase(ABCMeta):
    """
    Meta class for :class:`Model` class. This class automagically creates the
    necessary :attr:`Model._path` class variable if it is not already
    defined. The default path pattern is ``/modelname/{id}``.

    """

    def __new__(mcs, name, bases, attrs):
        new_class = super(ModelBase, mcs).__new__(mcs, name, bases, attrs)

        if name == 'Model':  # prevent unnecessary base work
            return new_class

        # don't override if defined
        if not new_class._path:
            new_class._path = u'/{0}/{{id}}'.format(quote(name.lower()))

        if not isinstance(new_class._pk, tuple):  # make sure it is a tuple
            new_class._pk = (new_class._pk,)

        new_class.update = new_class.update_with_patch if \
            new_class._update_method == 'PATCH' else new_class.update_with_put

        return new_class


class WrappedList(list):
    """
    Wrapped list implementation to dynamically create models as someone tries
    to access an item or a slice in the list. Returns a generator instead, when
    someone tries to iterate over the whole list.

    """

    def __init__(self, iterable, wrapper):
        super(self.__class__, self).__init__(iterable)
        self.__wrapper = wrapper

    def __getitem__(self, key):
        item = super(self.__class__, self).__getitem__(key)
        # check if we need to wrap the item, or if this is a slice, then check
        # if we need to wrap any item in the slice
        should_wrap = (isinstance(item, dict) or isinstance(key, slice) and
                       any(isinstance(it, dict) for it in item))

        if should_wrap:
            item = ([self.__wrapper(_) for _ in item]
                    if isinstance(key, slice) else self.__wrapper(item))

            self[key] = item  # cache wrapped item/slice

        return item

    def __getslice__(self, i, j):
        # We need this implementation for backwards compatibility.
        items = super(self.__class__, self).__getslice__(i, j)
        if any(isinstance(it, dict) for it in items):
            items = [self.__wrapper(_) for _ in items]
            self[i:j] = items  # cache wrapped slice
        return items

    def __iter__(self):
        # Call the base __iter__ to avoid infinite recursion and then simply
        # return an iterator.
        iterator = super(self.__class__, self).__iter__()
        return (self.__wrapper(item) for item in iterator)

    def __contains__(self, item):
        # Not very performant but necessary to use Model instances as operands
        # for the in operator.
        return item in iter(self)


class LazyList(object):
    """
    Lazy list implementation for continuous iteration over very large lists
    such as commits in a large repository. This is essentially a chained and
    structured generator. No caching and memoization at all since the intended
    usage is for small number of iterations.

    """

    def __init__(self, wrapper, fetcher):
        self.__wrapper = wrapper
        self.__fetcher = fetcher

    def __iter__(self):
        fetcher = self.__fetcher
        while fetcher:
            # fetcher is stored locally to prevent interference between
            # possible multiple iterations going at once
            data, fetcher = fetcher()  # this part never gets hit if the below
            # loop is not exhausted.
            for item in data:
                yield self.__wrapper(item)


class Auth(requests.auth.AuthBase):
    """
    Abstract base class for all custom authentication classes to be used with
    pyresto. See `Requests Documentation <http://docs.python-requests.org/en/
    latest/user/advanced/#custom-authentication>`_ for more info.
    """
    __metaclass__ = ABCMeta

    @abstractmethod
    def __call__(self, r):
        return r


class AuthList(dict):
    """
    An "attribute dict" which is basically a dict where item access can be done
    via attributes just like normal classes. Implementation taken from
    `StackOverflow <http://stackoverflow.com/questions/4984647/accessing
    -dict-keys-like-an-attribute-in-python>`_ and the class is used for
    defining authentication methods available for a given api. See
    :data:`apis.github.auths` for example usage.

    .. literalinclude:: ../pyresto/apis/github/models.py
        :lines: 102-103

    """
    def __getattr__(self, attr):
        return self[attr]

    def __setattr__(self, attr, value):
        self[attr] = value


def enable_auth(supported_types, base_model, default_type):
    """
    A "global authentication enabler" function generator. See
    :func:`apis.github.auth` for example usage.

    .. literalinclude:: ../pyresto/apis/github/models.py
        :lines: 105-106

    :param supported_types: A dict of supported types as ``"name": AuthClass``
                            pairs
    :type supported_types: dict

    :param base_model: The base model to set the :attr:`Model._auth` on
    :type base_model: :class:`Model`

    :param default_type: Default authentication type's name
    :type default_type: string

    :returns: An ``auth`` function that passes the arguments other then
              ``type`` to the given authentication type's constructor. Uses the
              default authentication class if ``type`` is omitted.
    :rtype: ``function(type=default_type, **kwargs)``
    """
    def auth(type=default_type, **kwargs):
        if type is None:
            base_model._auth = None
            return

        if type not in supported_types:
            raise PyrestoInvalidAuthTypeException('Unsupported auth type: {0}'
                                                  .format(type))

        base_model._auth = supported_types[type](**kwargs)

    return auth


class Relation(object):
    """Base class for all relation types."""


class Many(Relation):
    """
    Class for 'many' :class:`Relation` type which is essentially a collection
    for a certain model. Needs a base :class:`Model` for the collection and a
    `path` to get the collection from. Falls back to provided model's
    :attr:`Model.path` if not provided.

    """

    def __init__(self, model, path=None, lazy=False, preprocessor=None):
        """
        Constructor for Many relation instances.

        :param model: The model class that each instance in the collection
                      will be a member of.
        :type model: Model
        :param path: (optional) The unicode path to fetch the collection items,
                     if different than :attr:`Model._path`, which usually is.
        :type path: string or None

        :param lazy: (optional) A boolean indicator to determine the type of
                     the :class:`Many` field. Normally, it will be a
                     :class:`WrappedList` which is essentially a list. Use
                     ``lazy=True`` if the number of items in the collection
                     will be uncertain or very large which will result in a
                     :class:`LazyList` property which is practically a
                     generator.
        :type lazy: boolean

        """

        self.__model = model
        self.__path = path or model._path
        self.__lazy = lazy
        self.__preprocessor = preprocessor
        self.__cache = dict()

    def _with_owner(self, owner):
        """
        A function factory method which returns a mapping/wrapping function.
        The returned function creates a new instance of the :class:`Model` that
        the :class:`Relation` is defined with, sets its owner and
        "automatically fetched" internal flag and returns it.

        :param owner: The owner Model for the collection and its items.
        :type owner: Model

        """

        def mapper(data):
            if isinstance(data, dict):
                instance = self.__model(**data)
                instance._pyresto_owner = owner
                return instance
            elif isinstance(data, self.__model):
                return data
            else:
                raise TypeError("Invalid type passed to Many.")

        return mapper

    def __sanitize_data(self, data):
        if not data:
            return list()
        elif self.__preprocessor:
            return self.__preprocessor(data)
        return data

    def __make_fetcher(self, url, instance):
        """
        A function factory method which creates a simple fetcher function for
        the :class:`Many` relation, that is used internally. The
        :meth:`Model._rest_call` method defined on the models is expected to
        return the data and a continuation URL if there is any. This method
        generates a bound, fetcher function that calls the internal
        :meth:`Model._rest_call` function on the :class:`Model`, and processes
        its results to satisfy the requirements explained above.

        :param url: The url which the fetcher function will be bound to.
        :type url: unicode

        """

        def fetcher():
            data, new_url = self.__model._rest_call(url=url,
                                                    auth=instance._auth,
                                                    fetch_all=False)
            # Note the fetch_all=False in the call above, since this method is
            # intended for iterative LazyList calls.
            data = self.__sanitize_data(data)

            new_fetcher = self.__make_fetcher(new_url,
                                              instance) if new_url else None
            return data, new_fetcher

        return fetcher

    def __get__(self, instance, owner):
        # This method is called whenever a field defined as Many is tried to
        # be accessed. There is also another usage which lacks an object
        # instance in which case this simply returns the Model class then.
        if not instance:
            return self.__model

        cache = self.__cache
        if instance not in cache:
            model = self.__model

            path = self.__path.format(**instance._footprint)

            if self.__lazy:
                cache[instance] = LazyList(self._with_owner(instance),
                                           self.__make_fetcher(path, instance))
            else:
                data, next_url = model._rest_call(url=path,
                                                  auth=instance._auth)
                cache[instance] = WrappedList(self.__sanitize_data(data),
                                              self._with_owner(instance))
        return cache[instance]


class Foreign(Relation):
    """
    Class for 'foreign' :class:`Relation` type which is essentially a reference
    to a certain :class:`Model`. Needs a base :class:`Model` for obvious
    reasons.

    """

    def __init__(self, model, key_property=None, key_extractor=None,
                 embedded=False):
        """
        Constructor for the :class:`Foreign` relations.

        :param model: The model class for the foreign resource.
        :type model: Model

        :param key_property: (optional) The name of the property on the base
                             :class:`Model` which contains the id for the
                             foreign model.
        :type key_property: string or None

        :param key_extractor: (optional) The function that will extract the id
                              of the foreign model from the provided
                              :class:`Model` instance. This argument is
                              provided to make it possible to handle complex id
                              extraction operations for foreign fields.
        :type key_extractor: function(model)

        """

        self.__model = model
        self.__cache = dict()
        self.__embedded = embedded and not key_extractor

        self.__key_property = key_property or '__' + model.__name__.lower()

        if key_extractor:
            self.__key_extractor = key_extractor
        elif not embedded:
            def extract(instance):
                footprint = instance._footprint
                ids = list()

                for k in self.__model._pk[:-1]:
                    ids.append(footprint[k] if k in footprint
                               else getattr(instance, k))

                item, key = re.match(r'(\w+)(?:\[(\w+)\])?',
                                     key_property).groups()
                item = getattr(instance, item)
                ids.append(item[key] if key else item)

                return tuple(ids)

            self.__key_extractor = extract

    def __get__(self, instance, owner):
        # Please see Many.__get__ for more info on this method.
        if not instance:
            return self.__model

        if instance not in self.__cache:
            if self.__embedded:
                self.__cache[instance] = self.__model(
                    **getattr(instance, self.__key_property))
                self.__cache[instance]._auth = instance._auth
            else:
                self.__cache[instance] = self.__model.get(
                    *self.__key_extractor(instance), auth=instance._auth)

            self.__cache[instance]._pyresto_owner = instance

        return self.__cache[instance]


class Model(object):
    """
    The base model class where every data model using pyresto should be
    inherited from. Uses :class:`ModelBase` as its metaclass for various
    reasons explained in :class:`ModelBase`.

    """

    __metaclass__ = ModelBase

    _update_method = 'PATCH'

    __footprint = None

    __pk_vals = None

    _changed = None

    #: The class variable that holds the bae uel for the API endpoint for the
    #: :class:`Model`. This should be a "full" URL including the scheme, port
    #: and the initial path if there is any.
    _url_base = None

    #: The class variable that holds the path to be used to fetch the instance
    #: from the server. It is a format string using the new format notation
    #: defined for :meth:`str.format`. The primary key will be passed under the
    #: same name defined in the :attr:`_pk` property and any other named
    #: parameters passed to the :meth:`Model.get` or the class constructor will
    #: be available to this string for formatting.
    _path = None

    #: The class variable that holds the default authentication object to be
    #: passed to :mod:`requests`. Can be overridden on either class or instance
    #: level for convenience.
    _auth = None

    @classmethod
    def _continuator(cls, response):
        """
        The class method which receives the response from the server. This
        method is expected to return a continuation URL for the fetched
        resource, if there is any (like the next page's URL for paginated
        content) and ``None`` otherwise. The default implementation uses the
        standard HTTP link header and returns the url provided under the label
        "next" for continuation and ``None`` if it cannot find this label.

        :param response: The response for the HTTP request made to fetch the
                         resources.
        :type response: :class:`requests.Response`

        """

        return response.links.get('next', None)

    #: The class method which receives the class object and the body text of
    #: the server response to be parsed. It is expected to return a
    #: dictionary object having the properties of the related model. Defaults
    #: to a "staticazed" version of :func:`json.loads` so it is not necessary
    #: to override it if the response type is valid JSON.
    _parser = staticmethod(json.loads)

    #: The class method which receives the class object and a property dict of
    #: an instance to be serialized. It is expected to return a string which
    #: will be sent to the server on modification requests such as PATCH or
    #: CREATE. Defaults to a "staticazed" version of :func:`json.loads` so it
    #: is not necessary to override it if the response type is valid JSON.
    _serializer = staticmethod(json.dumps)

    @abstractproperty
    def _pk(self):
        """
        The class variable where the attribute name for the primary key for the
        :class:`Model` is stored as a string. This property is required and not
        providing a default is intentional to force developers to explicitly
        define it on every :class:`Model` class.

        """

    #: The instance variable which is used to determine if the :class:`Model`
    #: instance is filled from the server or not. It can be modified for
    #: certain usages but this is not suggested. If :attr:`_fetched` is
    #: ``False`` when an attribute, that is not in the class dictionary, tried
    #: to be accessed, the :meth:`__fetch` method is called before raising an
    #: :exc:`AttributeError`.
    _fetched = False

    #: The instance variable which holds the additional named get parameters
    #: provided to the :meth:`Model.get` to fetch the instance. It is used
    #: internally by the :class:`Relation` classes to get more info about the
    #: current :class:`Model` instance while fetching its related resources.
    _get_params = dict()

    def __init__(self, **kwargs):
        """
        Constructor for model instances. All named parameters passed to this
        method are bound to the newly created instance. Any property names
        provided at this level which are interfering with the predefined class
        relations (especially for :class:`Foreign` fields) are prepended "__"
        to avoid conflicts and to be used by the related relation class. For
        instance if your class has ``father = Foreign(Father)`` and ``father``
        is provided to the constructor, its value is saved under ``__father``
        to be used by the :class:`Foreign` relationship class as the id of the
        foreign :class:`Model`.
        """

        self.__dict__.update(kwargs)

        cls = self.__class__
        overlaps = set(cls.__dict__) & set(kwargs)

        for item in overlaps:
            if issubclass(getattr(cls, item), Model):
                self.__dict__['__' + item] = self.__dict__.pop(item)

        self._changed = set()

    @property
    def _id(self):
        """A property that returns the instance's primary key value."""
        if self.__pk_vals:
            return self.__pk_vals[-1]
        else:  # assuming last pk is defined on self!
            return getattr(self, self._pk[-1])

    @property
    def _pk_vals(self):
        if not self.__pk_vals:
            if hasattr(self, '_pyresto_owner'):
                self.__pk_vals = self.\
                    _pyresto_owner._pk_vals[:len(self._pk) - 1] + (self._id,)
            else:
                self.__pk_vals = (None,) * (len(self._pk) - 1) + (self._id,)

        return self.__pk_vals

    @_pk_vals.setter
    def _pk_vals(self, value):
        if len(value) == len(self._pk):
            self.__pk_vals = tuple(value)
        else:
            raise ValueError

    @property
    def _footprint(self):
        if not self.__footprint:
            self.__footprint = dict(zip(self._pk, self._pk_vals))
            self.__footprint['self'] = self

        return self.__footprint

    @property
    def _current_path(self):
        return self._path.format(**self._footprint)

    @classmethod
    def _get_sanitized_url(cls, url):
        return urlparse.urljoin(cls._url_base, url)

    @classmethod
    def _rest_call(cls, url, method='GET', fetch_all=True, **kwargs):
        """
        A method which handles all the heavy HTTP stuff by itself. This is
        actually a private method but to let the instances and derived classes
        to call it, is made ``protected`` using only a single ``_`` prefix.

        All undocumented keyword arguments are passed to the HTTP request as
        keyword arguments such as method, url etc.

        :param fetch_all: (optional) Determines if the function should
                          recursively fetch any "paginated" resource or simply
                          return the downloaded and parsed data along with a
                          continuation URL.
        :type fetch_all: boolean

        :returns: Returns a tuple where the first part is the parsed data from
                  the server using :attr:`Model._parser`, and the second half
                  is the continuation URL extracted using
                  :attr:`Model._continuator` or ``None`` if there isn't any.
        :rtype: tuple

        """

        url = cls._get_sanitized_url(url)

        if cls._auth is not None and 'auth' not in kwargs:
            kwargs['auth'] = cls.auth

        if method in ALLOWED_HTTP_METHODS:
            response = requests.request(method.lower(), url, verify=True,
                                        **kwargs)
        else:
            raise PyrestoInvalidRestMethodException(
                'Invalid method "{0:s}" is used for the HTTP request. Can only'
                'use the following: {1!s}'.format(method,
                                                  ALLOWED_HTTP_METHODS))

        result = collections.namedtuple('result', 'data continuation_url')
        if 200 <= response.status_code < 300:
            continuation_url = cls._continuator(response)
            response_data = response.text
            data = cls._parser(response_data) if response_data else None
            if continuation_url:
                logging.debug('Found more at: %s', continuation_url)
                if fetch_all:
                    kwargs['url'] = continuation_url
                    data += cls._rest_call(**kwargs).data
                else:
                    return result(data, continuation_url)
            return result(data, None)
        else:
            msg = '%s returned HTTP %d: %s\nResponse\nHeaders: %s\nBody: %s'
            logging.error(msg, url, response.status_code, kwargs,
                          response.headers, response.text)

            raise PyrestoServerResponseException('Server response not OK. '
                                                 'Response code: {0:d}'
                                                 .format(response.status_code))

    def __fetch(self):
        data, next_url = self._rest_call(url=self._current_path,
                                         auth=self._auth)

        if data:
            self.__dict__.update(data)

            cls = self.__class__
            overlaps = set(cls.__dict__) & set(data)

            for item in overlaps:
                if issubclass(getattr(cls, item), Model):
                    self.__dict__['__' + item] = self.__dict__.pop(item)

            self._fetched = True

    def __getattr__(self, name):
        if self._fetched:  # if we fetched and still don't have it, no luck!
            raise AttributeError
        self.__fetch()
        return getattr(self, name)  # try again after fetching

    def __setattr__(self, key, value):
        if not key.startswith('_'):
            self._changed.add(key)
        super(Model, self).__setattr__(key, value)

    def __delattr__(self, item):
        raise PyrestoInvalidOperationException(
            "Del method on Pyresto models is not supported.")

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self._id == other._id

    def __repr__(self):
        if self._path:
            descriptor = self._current_path
        else:
            descriptor = ' - {0}'.format(self._footprint)

        return '<Pyresto.Model.{0} [{1}]>'.format(self.__class__.__name__,
                                                  descriptor)

    @classmethod
    def read(cls, *args, **kwargs):
        """
        The class method that fetches and instantiates the resource defined by
        the provided pk value. Any other extra keyword arguments are used to
        format the :attr:`Model._path` variable to construct the request URL.

        :param pk: The primary key value for the requested resource.
        :type pk: string

        :rtype: :class:`Model` or None

        """

        auth = kwargs.pop('auth', cls._auth)

        ids = dict(zip(cls._pk, args))
        path = cls._path.format(**ids)
        data = cls._rest_call(url=path, auth=auth).data

        if not data:
            return None

        instance = cls(**data)
        instance._pk_vals = args
        instance._fetched = True
        if auth:
            instance._auth = auth

        return instance

    @classmethod
    @normalize_auth
    @assert_class_instance
    def update_with_patch(cls, instance, keys=None, auth=None):
        if keys:
            keys &= instance._changed
        else:
            keys = instance._changed

        data = dict((key, instance.__dict__[key]) for key in keys)
        path = instance._current_path
        resp = cls._rest_call(method="PATCH", url=path, auth=auth,
                              data=cls._serializer(data)).data
        instance.__dict__.update(resp)
        instance._changed -= keys

        return instance

    @classmethod
    @normalize_auth
    @assert_class_instance
    def update_with_put(cls, instance, auth=None):
        data = instance.__dict__.copy()
        path = instance._current_path
        resp = cls._rest_call(method="PUT", url=path, auth=auth,
                              data=cls._serializer(data)).data
        instance.__dict__.update(resp)
        instance._changed.clear()

        return instance

    @classmethod
    @normalize_auth
    @assert_class_instance
    def delete(cls, instance, auth=None):
        cls._rest_call(method="DELETE", url=instance._current_path, auth=auth)

        return True  # will raise error if server responds with non 2xx
