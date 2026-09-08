/**
 * The document store, kept at its old import path.
 *
 * It now lives in `./document`, split into a document/history slice, a
 * selection/sketch slice and a compute/job slice — three groups whose state has
 * three different lifetimes, previously enforced by remembering to spread
 * `...STALE` at twenty-odd call sites.
 *
 * This file is the compatibility facade. Every panel imports
 * `useDocumentStore` from here and none of them changed, which is what made the
 * split reviewable: the alternative was one commit touching forty components.
 * New code can import from `./document` directly.
 */

export { useDocumentStore, historySyncSettled, JobLostError, pollJob } from './document'
export type {
  ComputeSlice,
  DocumentSlice,
  DocumentState,
  HistoryEntry,
  OperationFailure,
  SelectionSlice,
  SketchSpec,
  TopologyState,
} from './document'
