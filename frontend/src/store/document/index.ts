/**
 * The three slices, combined into the store every panel already imports.
 *
 * The facade is the point of doing it this way. `useDocumentStore` keeps its
 * exact shape, so nothing that consumes it changed — the split buys the slices
 * their own files and their own rules about what survives what, without a
 * migration across forty components that would have to land in one commit.
 */

import { create } from 'zustand'

import { createComputeSlice } from './computeSlice'
import { createDocumentSlice } from './documentSlice'
import { createSelectionSlice } from './selectionSlice'
import type { DocumentState } from './shared'

export const useDocumentStore = create<DocumentState>((set, get) => ({
  ...createDocumentSlice(set, get),
  ...createSelectionSlice(set, get),
  ...createComputeSlice(set, get),
}))

export { historySyncSettled } from './documentSlice'
export { JobLostError, pollJob } from './polling'
export type {
  ComputeSlice, DocumentSlice, DocumentState, HistoryEntry, OperationFailure,
  SelectionSlice, SketchSpec, TopologyState,
} from './shared'
