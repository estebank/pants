// Copyright 2017 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

use std;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use tokio::runtime::Runtime;

use core::TypeId;
use externs;
use fs::{safe_create_dir_all_ioerror, PosixFS, ResettablePool, Store};
use graph::{EntryId, Graph};
use handles::maybe_drain_handles;
use nodes::{Node, NodeFuture};
use process_execution::{self, BoundedCommandRunner, CommandRunner};
use resettable::Resettable;
use rule_graph::RuleGraph;
use tasks::Tasks;
use types::Types;

///
/// The core context shared (via Arc) between the Scheduler and the Context objects of
/// all running Nodes.
///
/// Over time, most usage of `ResettablePool` (which wraps use of blocking APIs) should migrate
/// to the Tokio `Runtime`. The next candidate is likely to be migrating PosixFS to tokio-fs once
/// https://github.com/tokio-rs/tokio/issues/369 is resolved.
///
pub struct Core {
  pub graph: Graph,
  pub tasks: Tasks,
  pub rule_graph: RuleGraph,
  pub types: Types,
  pub fs_pool: Arc<ResettablePool>,
  pub runtime: Resettable<Arc<Runtime>>,
  pub store: Store,
  pub vfs: PosixFS,
  pub command_runner: BoundedCommandRunner,
}

impl Core {
  pub fn new(
    root_subject_types: Vec<TypeId>,
    tasks: Tasks,
    types: Types,
    build_root: &Path,
    ignore_patterns: Vec<String>,
    work_dir: &Path,
    remote_store_server: Option<String>,
    remote_execution_server: Option<String>,
  ) -> Core {
    let mut snapshots_dir = PathBuf::from(work_dir);
    snapshots_dir.push("snapshots");

    let fs_pool = Arc::new(ResettablePool::new("io-".to_string()));
    let runtime = Resettable::new(|| {
      Arc::new(Runtime::new().unwrap_or_else(|e| panic!("Could not initialize Runtime: {:?}", e)))
    });

    let store_path = match std::env::home_dir() {
      Some(home_dir) => home_dir.join(".cache").join("pants").join("lmdb_store"),
      None => panic!("Could not find home dir"),
    };

    let store = safe_create_dir_all_ioerror(&store_path)
      .map_err(|e| format!("Error making directory {:?}: {:?}", store_path, e))
      .and_then(|()| {
        match remote_store_server {
          Some(address) => Store::with_remote(
            store_path,
            fs_pool.clone(),
            address,
            // TODO: Allow configuration of all of the below:
            1,
            1024 * 1024,
            std::time::Duration::from_secs(60),
          ),
          None => Store::local_only(store_path, fs_pool.clone()),
        }
      })
      .unwrap_or_else(|e| panic!("Could not initialize Store: {:?}", e));

    let underlying_command_runner: Box<process_execution::CommandRunner> =
      match remote_execution_server {
        Some(address) => Box::new(process_execution::remote::CommandRunner::new(
          address,
          1,
          store.clone(),
        )),
        None => Box::new(process_execution::local::CommandRunner::new(
          store.clone(),
          fs_pool.clone(),
        )),
      };

    // TODO: Allow configuration of process concurrency.
    let command_runner = BoundedCommandRunner::new(underlying_command_runner, 16);

    let rule_graph = RuleGraph::new(&tasks, root_subject_types);

    Core {
      graph: Graph::new(),
      tasks: tasks,
      rule_graph: rule_graph,
      types: types,
      fs_pool: fs_pool.clone(),
      runtime: runtime,
      store: store,
      // FIXME: Errors in initialization should definitely be exposed as python
      // exceptions, rather than as panics.
      vfs: PosixFS::new(build_root, fs_pool, ignore_patterns).unwrap_or_else(|e| {
        panic!("Could not initialize VFS: {:?}", e);
      }),
      command_runner: command_runner,
    }
  }

  pub fn pre_fork(&self) {
    self.fs_pool.reset();
    self.store.reset_prefork();
    self.runtime.reset();
    self.command_runner.reset_prefork();
  }
}

#[derive(Clone)]
pub struct Context {
  pub entry_id: EntryId,
  pub core: Arc<Core>,
}

impl Context {
  pub fn new(entry_id: EntryId, core: Arc<Core>) -> Context {
    Context {
      entry_id: entry_id,
      core: core,
    }
  }

  ///
  /// Get the future value for the given Node implementation.
  ///
  pub fn get<N: Node>(&self, node: N) -> NodeFuture<N::Output> {
    // TODO: Odd place for this... could do it periodically in the background?
    maybe_drain_handles().map(|handles| {
      externs::drop_handles(handles);
    });
    self.core.graph.get(self.entry_id, self, node)
  }
}

pub trait ContextFactory {
  fn create(&self, entry_id: EntryId) -> Context;
}

impl ContextFactory for Context {
  ///
  /// Clones this Context for a new EntryId. Because the Core of the context is an Arc, this
  /// is a shallow clone.
  ///
  fn create(&self, entry_id: EntryId) -> Context {
    Context {
      entry_id: entry_id,
      core: self.core.clone(),
    }
  }
}
