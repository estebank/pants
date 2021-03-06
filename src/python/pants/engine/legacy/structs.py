# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import logging
from abc import abstractproperty

from six import string_types

from pants.base.deprecated import deprecated_conditional
from pants.engine.addressable import addressable_list
from pants.engine.fs import PathGlobs
from pants.engine.objects import Locatable
from pants.engine.struct import Struct, StructWithDeps
from pants.source import wrapped_globs
from pants.util.contextutil import exception_logging
from pants.util.meta import AbstractClass
from pants.util.objects import Exactly, datatype


logger = logging.getLogger(__name__)


class TargetAdaptor(StructWithDeps):
  """A Struct to imitate the existing Target.

  Extends StructWithDeps to add a `dependencies` field marked Addressable.
  """

  def get_sources(self):
    """Returns target's non-deferred sources if exists or the default sources if defined.

    NB: once ivy is implemented in the engine, we can fetch sources natively here, and/or
    refactor how deferred sources are implemented.
      see: https://github.com/pantsbuild/pants/issues/2997
    """
    sources = getattr(self, 'sources', None)
    # N.B. Here we check specifically for `sources is None`, as it's possible for sources
    # to be e.g. an explicit empty list (sources=[]).
    if sources is None and self.default_sources_globs is not None:
      return Globs(*self.default_sources_globs,
                    spec_path=self.address.spec_path,
                    exclude=self.default_sources_exclude_globs or [])
    return sources

  @property
  def field_adaptors(self):
    """Returns a tuple of Fields for captured fields which need additional treatment."""
    with exception_logging(logger, 'Exception in `field_adaptors` property'):
      sources = self.get_sources()
      if not sources:
        return tuple()
      base_globs = BaseGlobs.from_sources_field(sources, self.address.spec_path)
      path_globs = base_globs.to_path_globs(self.address.spec_path)
      return (SourcesField(self.address, 'sources', base_globs.filespecs, base_globs, path_globs),)

  @property
  def default_sources_globs(self):
    return None

  @property
  def default_sources_exclude_globs(self):
    return None


class Field(object):
  """A marker for Target(Adaptor) fields for which the engine might perform extra construction."""


class SourcesField(datatype(['address', 'arg', 'filespecs', 'base_globs', 'path_globs']), Field):
  """Represents the `sources` argument for a particular Target.

  Sources are currently eagerly computed in-engine in order to provide the `BuildGraph`
  API efficiently; once tasks are explicitly requesting particular Products for Targets,
  lazy construction will be more natural.
    see https://github.com/pantsbuild/pants/issues/3560

  :param address: The Address of the TargetAdaptor for which this field is an argument.
  :param arg: The name of this argument: usually 'sources', but occasionally also 'resources' in the
    case of python resource globs.
  :param filespecs: The merged filespecs dict the describes the paths captured by this field.
  :param path_globs: A PathGlobs describing included files.
  """

  def __hash__(self):
    return hash((self.address, self.arg))

  def __repr__(self):
    return str(self)

  def __str__(self):
    return '{}(address={}, input_globs={}, arg={}, filespecs={!r})'.format(
      type(self).__name__, self.address, self.base_globs, self.arg, self.filespecs)


class JavaLibraryAdaptor(TargetAdaptor):
  @property
  def default_sources_globs(self):
    return ('*.java',)

  @property
  def default_sources_exclude_globs(self):
    return JunitTestsAdaptor.java_test_globs


class ScalaLibraryAdaptor(TargetAdaptor):
  @property
  def default_sources_globs(self):
    return ('*.scala',)

  @property
  def default_sources_exclude_globs(self):
    return JunitTestsAdaptor.scala_test_globs


class JunitTestsAdaptor(TargetAdaptor):
  java_test_globs = ('*Test.java',)
  scala_test_globs = ('*Test.scala', '*Spec.scala')

  @property
  def default_sources_globs(self):
    return self.java_test_globs + self.scala_test_globs


class BundlesField(datatype(['address', 'bundles', 'filespecs_list', 'path_globs_list']), Field):
  """Represents the `bundles` argument, each of which has a PathGlobs to represent its `fileset`."""

  def __hash__(self):
    return hash(self.address)


class BundleAdaptor(Struct):
  """A Struct to capture the args for the `bundle` object.

  Bundles have filesets which we need to capture in order to execute them in the engine.

  TODO: Bundles should arguably be Targets, but that distinction blurs in the `exp` examples
  package, where a Target is just a collection of configuration.
  """


class AppAdaptor(TargetAdaptor):
  def __init__(self, bundles=None, **kwargs):
    """
    :param list bundles: A list of `BundleAdaptor` objects
    """
    super(AppAdaptor, self).__init__(**kwargs)
    self.bundles = bundles

  @addressable_list(Exactly(BundleAdaptor))
  def bundles(self):
    """The BundleAdaptors for this JvmApp."""
    return self.bundles

  @property
  def field_adaptors(self):
    with exception_logging(logger, 'Exception in `field_adaptors` property'):
      field_adaptors = super(AppAdaptor, self).field_adaptors
      if getattr(self, 'bundles', None) is None:
        return field_adaptors

      bundles_field = self._construct_bundles_field()
      return field_adaptors + (bundles_field,)

  def _construct_bundles_field(self):
    filespecs_list = []
    path_globs_list = []
    for bundle in self.bundles:
      # NB: if a bundle has a rel_path, then the rel_root of the resulting file globs must be
      # set to that rel_path.
      rel_root = getattr(bundle, 'rel_path', self.address.spec_path)

      base_globs = BaseGlobs.from_sources_field(bundle.fileset, rel_root)
      # TODO: we want to have this field set from the global option --glob-expansion-failure, or
      # something set on the target. Should we move --glob-expansion-failure to be a bootstrap
      # option? See #5864.
      path_globs = base_globs.to_path_globs(rel_root)

      filespecs_list.append(base_globs.filespecs)
      path_globs_list.append(path_globs)
    return BundlesField(self.address,
                        self.bundles,
                        filespecs_list,
                        path_globs_list)


class RemoteSourcesAdaptor(TargetAdaptor):
  def __init__(self, dest=None, **kwargs):
    """
    :param dest: A target constructor.
    """
    if not isinstance(dest, string_types):
      dest = dest._type_alias
    super(RemoteSourcesAdaptor, self).__init__(dest=dest, **kwargs)


class PythonTargetAdaptor(TargetAdaptor):
  @property
  def field_adaptors(self):
    with exception_logging(logger, 'Exception in `field_adaptors` property'):
      field_adaptors = super(PythonTargetAdaptor, self).field_adaptors
      if getattr(self, 'resources', None) is None:
        return field_adaptors
      base_globs = BaseGlobs.from_sources_field(self.resources, self.address.spec_path)
      path_globs = base_globs.to_path_globs(self.address.spec_path)
      sources_field = SourcesField(self.address,
                                   'resources',
                                   base_globs.filespecs,
                                   base_globs,
                                   path_globs)
      return field_adaptors + (sources_field,)


class PythonLibraryAdaptor(PythonTargetAdaptor):
  @property
  def default_sources_globs(self):
    return ('*.py',)

  @property
  def default_sources_exclude_globs(self):
    return PythonTestsAdaptor.python_test_globs


class PythonTestsAdaptor(PythonTargetAdaptor):
  python_test_globs = ('test_*.py', '*_test.py')

  @property
  def default_sources_globs(self):
    return self.python_test_globs


class GoTargetAdaptor(TargetAdaptor):

  @property
  def default_sources(self):
    # Go has always used implicit_sources: override to ignore the option.
    return True

  @property
  def default_sources_globs(self):
    # N.B. Go targets glob on `*` due to the way resources and .c companion files are handled.
    return ('*',)

  @property
  def default_sources_exclude_globs(self):
    return ('BUILD', 'BUILD.*')


class BaseGlobs(Locatable, AbstractClass):
  """An adaptor class to allow BUILD file parsing from ContextAwareObjectFactories."""

  @staticmethod
  def from_sources_field(sources, spec_path):
    """Return a BaseGlobs for the given sources field.

    `sources` may be None, a list/tuple/set, a string or a BaseGlobs instance.
    """
    if sources is None:
      return Files(spec_path=spec_path)
    elif isinstance(sources, BaseGlobs):
      return sources
    elif isinstance(sources, string_types):
      return Files(sources, spec_path=spec_path)
    elif isinstance(sources, (set, list, tuple)) and \
         all(isinstance(s, string_types) for s in sources):
      return Files(*sources, spec_path=spec_path)
    else:
      raise ValueError('Expected either a glob or list of literal sources: got: {}'.format(sources))

  @staticmethod
  def _filespec_for_exclude(raw_exclude, spec_path):
    if isinstance(raw_exclude, string_types):
      raise ValueError('Excludes of type `{}` are not supported: got "{}"'
                       .format(type(raw_exclude).__name__, raw_exclude))

    excluded_patterns = []
    for raw_element in raw_exclude:
      exclude_filespecs = BaseGlobs.from_sources_field(raw_element, spec_path).filespecs
      if exclude_filespecs.get('exclude', []):
        raise ValueError('Nested excludes are not supported: got {}'.format(raw_element))
      excluded_patterns.extend(exclude_filespecs.get('globs', []))
    return {'globs': excluded_patterns}

  @abstractproperty
  def path_globs_kwarg(self):
    """The name of the `PathGlobs` parameter corresponding to this BaseGlobs instance."""

  @abstractproperty
  def legacy_globs_class(self):
    """The corresponding `wrapped_globs` class for this BaseGlobs."""

  def __init__(self, *patterns, **kwargs):
    self._patterns = patterns
    self._kwargs = kwargs
    raw_spec_path = kwargs.pop('spec_path')
    self._file_globs = self.legacy_globs_class.to_filespec(patterns).get('globs', [])
    raw_exclude = kwargs.pop('exclude', [])
    self._excluded_file_globs = self._filespec_for_exclude(raw_exclude, raw_spec_path).get('globs', [])
    self._spec_path = raw_spec_path

    # `follow_links=True` is the default behavior for wrapped globs, so we pop the old kwarg
    # and warn here to bridge the gap from v1->v2 BUILD files.
    if kwargs.pop('follow_links', None) is not None:
      deprecated_conditional(
        lambda: True,
        '1.10.0.dev0',
        'Ignoring `follow_links` kwarg on glob in `{}`. Default behavior is to follow all links.'
          .format(self._spec_path)
      )

    if kwargs:
      raise ValueError('kwargs not supported for {}. Got: {}'.format(type(self), kwargs))

  @property
  def filespecs(self):
    """Return a filespecs dict representing both globs and excludes."""
    filespecs = {'globs': self._file_globs}
    exclude_filespecs = self._exclude_filespecs
    if exclude_filespecs:
      filespecs['exclude'] = exclude_filespecs
    return filespecs

  @property
  def _exclude_filespecs(self):
    if self._excluded_file_globs:
      return [{'globs': self._excluded_file_globs}]
    else:
      return []

  def to_path_globs(self, relpath):
    """Return a PathGlobs representing the included and excluded Files for these patterns."""
    return PathGlobs.create(relpath, self._file_globs, self._excluded_file_globs)

  def _gen_init_args_str(self):
    all_arg_strs = []
    positional_args = ', '.join([repr(p) for p in self._patterns])
    if positional_args:
      all_arg_strs.append(positional_args)
    keyword_args = ', '.join([
      '{}={}'.format(k, repr(v)) for k, v in self._kwargs.items()
    ])
    if keyword_args:
      all_arg_strs.append(keyword_args)

    return ', '.join(all_arg_strs)

  def __repr__(self):
    return '{}({})'.format(type(self).__name__, self._gen_init_args_str())

  def __str__(self):
    return '{}({})'.format(self.path_globs_kwarg, self._gen_init_args_str())


class Files(BaseGlobs):
  path_globs_kwarg = 'files'
  legacy_globs_class = wrapped_globs.Globs

  def __str__(self):
    return '[{}]'.format(', '.join(repr(p) for p in self._patterns))


class Globs(BaseGlobs):
  path_globs_kwarg = 'globs'
  legacy_globs_class = wrapped_globs.Globs


class RGlobs(BaseGlobs):
  path_globs_kwarg = 'rglobs'
  legacy_globs_class = wrapped_globs.RGlobs


class ZGlobs(BaseGlobs):
  path_globs_kwarg = 'zglobs'
  legacy_globs_class = wrapped_globs.ZGlobs
