import { api } from "../lib/api";
import type { RoutingCampaignIndex } from "../lib/types";
import { useRequest } from "./useRequest";

/** Read the bounded, verified routing-campaign index with the shared GET hook. */
export function useRoutingCampaigns() {
  return useRequest<RoutingCampaignIndex>(
    (signal) => api.listRoutingCampaigns(signal),
    [],
  );
}
