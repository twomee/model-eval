import type { Api } from '../services/api'
import type { ModelsResponse } from '../types/api'
import { type ResourceState, useResource } from './useResource'

export type ModelsState = ResourceState<ModelsResponse>

export function useModels(client: Pick<Api, 'listModels'>): ModelsState {
  return useResource(client.listModels)
}
