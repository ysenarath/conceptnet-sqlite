from __future__ import annotations

import os
import shutil
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Iterable, Set, Union

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from tqdm import auto as tqdm

from .vocab import Vocab
from .loader import KnowledgeLoader
from .models import Base, Edge, Node
from .triplet import NodeIndex, TripletStore

__all__ = [
    "KnowledgeBase",
]

logger = logging.getLogger(__name__)


def get_cache_dir(base_dir: Union[Path, str, None]) -> Path:
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir.absolute().expanduser()


def get_kb_path(
    name_or_path: str | Path,
    cache_dir: str | Path | None = None,
    create: bool = False,
    verbose: int = 1,
) -> Path:
    kb_cache_dir = get_cache_dir(cache_dir) / "kb"
    input_path = Path(name_or_path)
    input_path = input_path.with_name(f"{input_path.name}.db")
    if not input_path.exists() and isinstance(name_or_path, str):
        input_path = kb_cache_dir / f"{name_or_path}.db"
        # create the database if it does not exist
        if not input_path.exists() and create:
            input_engine = create_engine(f"sqlite:///{input_path}", echo=verbose > 2)
            Base.metadata.create_all(input_engine)
    elif input_path.exists():
        name_or_path = os.path.join("default", input_path.stem)
    else:
        raise ValueError("invalid path")
    return kb_cache_dir / f"{name_or_path}.db"


class KnowledgeBase:
    def __init__(
        self,
        database_name_or_path: str | Path,
        create: bool = False,
        cache_dir: Union[Path, str, None] = None,
        verbose: int = 1,
    ):
        # output path with checksum
        self.path = get_kb_path(
            database_name_or_path,
            cache_dir=cache_dir,
            create=create,
            verbose=verbose,
        )
        self.engine = create_engine(f"sqlite:///{self.path}", echo=verbose > 2)
        with self.session() as session:
            session.execute(text("PRAGMA journal_mode=WAL"))
        # create index if not exists
        self.index = self._get_or_create_index()
        self.node_index = NodeIndex(self.path.with_suffix(".node.db"))

    def get_node_ids_by_label(self, label: str) -> Iterable[str]:
        return self.label2index.get(label, set())

    def _create_label2index(self) -> dict[str, Set[int]]:
        label2index: dict[str, Set[int]] = {}
        with self.session() as session:
            for node in session.query(Node):
                if node.label not in label2index:
                    label2index[node.label] = set()
                label2index[node.label].add(node.id)
        return label2index

    def num_edges(self) -> int:
        with self.session() as session:
            return session.query(Edge).count()

    def _get_or_create_index(self) -> TripletStore:
        # populate index if not frozen (frozen means index is up-to-date)
        index_path = str(self.path.with_suffix("")) + "-index"
        index = TripletStore(index_path)
        if index.frozen:
            return index
        with self.session() as session:
            n_total = session.query(Edge).count()
            query = session.query(Edge.start_id, Edge.rel_id, Edge.end_id)
            pbar = tqdm.tqdm(query.yield_per(int(1e5)), total=n_total, desc="Indexing")
            index.add(pbar)
        index.frozen = True
        return index

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        yield Session(self.engine)

    def cleanup(self):
        if not os.path.exists(self.path):
            return
        os.remove(self.path)

    def iternodes(self, verbose: bool = False) -> Iterable[Node]:
        with self.session() as session:
            query = session.query(Node)
            pbar = tqdm.tqdm(query.all(), desc="Iterating Nodes", disable=not verbose)
            for node in pbar:
                yield node

    def get_vocab(self) -> Vocab:
        with self.session() as session:
            n_total = session.query(Node).count()
        # populate vocab if not frozen (frozen means vocab is up-to-date)
        vocab_db_path = self.path.with_name(self.path.stem + "-vocab.db")
        config = {
            "type": "sqlalchemy",
            "url": f"sqlite:///{vocab_db_path}",
        }
        if os.path.exists(vocab_db_path):
            vocab = Vocab(config)
            return vocab
        vocab_db_temp_path = self.path.with_name(self.path.stem + "-vocab.db.tmp")
        # remove the existing vocab database
        if os.path.exists(vocab_db_temp_path):
            os.remove(vocab_db_temp_path)
        tmp_config = {
            "type": "sqlalchemy",
            "url": f"sqlite:///{vocab_db_temp_path}",
        }
        vocab = Vocab(tmp_config)
        batch_size = int(1e4)
        with self.session() as session:
            query = session.query(Node)
            pbar = tqdm.tqdm(
                query.yield_per(batch_size),
                total=n_total,
                desc="Building Vocabulary",
            )
            nodes = []
            for i, node in enumerate(pbar):  # this will be done in parallel
                nodes.append(node)
                if i % batch_size == 0:
                    vocab.extend(nodes)
                    nodes = []
            if nodes:
                vocab.extend(nodes)
        del vocab
        # move the temporary database to the final location
        shutil.move(vocab_db_temp_path, vocab_db_path)
        vocab = Vocab(config)
        return vocab

    @classmethod
    def from_loader(cls, loader: KnowledgeLoader) -> KnowledgeBase:
        identifier = loader.config.identifier
        version = loader.config.version or "0.0.1"
        if not identifier.isidentifier():
            raise ValueError("identifier must be a valid Python identifier")
        database_name = os.path.join(identifier, f"{identifier}-v{version}")
        self = cls(database_name, create=True)
        with self.session() as session:
            session.execute(text("PRAGMA journal_mode=WAL"))
            for i, val in enumerate(loader.iterrows()):
                _ = Edge.from_dict(  # ignore the return value
                    val,
                    session=session,
                    commit=False,
                    namespace=loader.config.namespace,
                )
                if i % 100 == 0:
                    try:
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        raise e
            try:
                session.commit()
            except Exception as e:
                session.rollback()
                raise e
