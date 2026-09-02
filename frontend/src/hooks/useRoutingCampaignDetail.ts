import { api } from "../lib/api";
import type { RoutingCampaignDetail } from "../lib/types";
import { useRequest } from "./useRequest";

/** Abort stale campaign-detail reads when the selected campaign route changes. */
export function useRoutingCampaignDetail(campaignId: string | null | undefined) {
  return useRequest<RoutingCampaignDetail>(
    (signal) => {
      if (!campaignId) {
        return Promise.reject(new Error("No routing campaign was selected."));
      }
      return api.getRoutingCampaign(campaignId, signal);
    },
    [campaignId],
    { enabled: Boolean(campaignId) },
  );
}
