import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { QubaxPanel } from "../QubaxPanel";

const pushMock = vi.hoisted(() => vi.fn());
const confirmMock = vi.hoisted(() => vi.fn(async () => true));

const apiMock = vi.hoisted(() => ({
  qubax: {
    status: vi.fn(),
    setApiKey: vi.fn(),
    saveForm: vi.fn(),
    models: vi.fn(),
    marketPrices: vi.fn(),
    estimate: vi.fn(),
    modelDetail: vi.fn(),
    listJobs: vi.fn(),
    createBatch: vi.fn(),
    createJob: vi.fn(),
    getJob: vi.fn(),
    importJob: vi.fn(),
  },
  videos: { list: vi.fn() },
}));

vi.mock("../../api/client", () => ({
  api: apiMock,
  formatApiError: (e: unknown) => String(e),
}));

vi.mock("../../context/ToastContext", () => ({
  useToast: () => ({ push: pushMock }),
}));

vi.mock("../../context/ConfirmContext", () => ({
  useConfirm: () => confirmMock,
}));

vi.mock("../../hooks/usePollWhenVisible", () => ({
  usePollWhenVisible: () => {},
}));

describe("QubaxPanel", () => {
  beforeEach(() => {
    vi.useRealTimers();
    pushMock.mockReset();
    apiMock.qubax.status.mockReset();
    apiMock.qubax.setApiKey.mockReset();
    apiMock.qubax.models.mockReset();
    apiMock.qubax.listJobs.mockReset();
    apiMock.qubax.createBatch.mockReset();
    apiMock.qubax.createJob.mockReset();
    apiMock.qubax.importJob.mockReset();
    apiMock.videos.list.mockReset();
    apiMock.qubax.status.mockResolvedValue({
      configured: false,
      key_from_env: false,
    });
    apiMock.videos.list.mockResolvedValue([]);
    apiMock.qubax.listJobs.mockResolvedValue({ items: [], total: 0 });
    apiMock.qubax.saveForm.mockResolvedValue({ configured: true, key_from_env: false });
    apiMock.qubax.modelDetail.mockResolvedValue({});
    apiMock.qubax.marketPrices.mockResolvedValue({ prices: {} });
    apiMock.qubax.estimate.mockResolvedValue({
      items: [],
      jobs_count: 0,
      total_usd: null,
    });
  });

  it("shows api key field when not configured", async () => {
    render(
      <MemoryRouter>
        <QubaxPanel />
      </MemoryRouter>
    );
    expect(await screen.findByTestId("qubax-api-key")).toBeInTheDocument();
  });

  it("calls setApiKey on save", async () => {
    apiMock.qubax.setApiKey.mockResolvedValue({
      configured: true,
      key_from_env: false,
      balance: 1,
    });
    apiMock.qubax.models.mockResolvedValue({ presets: [], models: [] });
    render(
      <MemoryRouter>
        <QubaxPanel />
      </MemoryRouter>
    );
    const input = await screen.findByTestId("qubax-api-key");
    fireEvent.change(input, { target: { value: "qbx_live_test" } });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
    await waitFor(() =>
      expect(apiMock.qubax.setApiKey).toHaveBeenCalledWith("qbx_live_test")
    );
  });

  it("debounces saveForm when prompt changes", async () => {
    apiMock.qubax.status.mockResolvedValue({
      configured: true,
      key_from_env: false,
      balance: 5,
      settings: {
        api_public_base_url: "",
        api_public_source: "none",
        api_public_configured: false,
        default_model: "video-gen",
        default_model_i2v: "uncensored-i2v-pro",
      },
    });
    apiMock.qubax.models.mockResolvedValue({ presets: [], models: [] });
    apiMock.qubax.marketPrices.mockResolvedValue({ prices: {} });
    render(
      <MemoryRouter>
        <QubaxPanel />
      </MemoryRouter>
    );
    await screen.findByTestId("qubax-prompt");
    await new Promise((r) => setTimeout(r, 120));
    fireEvent.change(screen.getByTestId("qubax-prompt"), { target: { value: "autosave me" } });
    await waitFor(() => expect(apiMock.qubax.saveForm).toHaveBeenCalled(), { timeout: 3000 });
  });

  it("batch button triggers createBatch when configured", async () => {
    apiMock.qubax.status.mockResolvedValue({
      configured: true,
      key_from_env: false,
      balance: 5,
    });
    apiMock.qubax.models.mockResolvedValue({
      presets: [{ id: "video-gen", label: "Video Gen" }],
      models: [],
    });
    apiMock.qubax.createBatch.mockResolvedValue({
      items: [{ id: 1, external_id: "x", model: "video-gen", prompt: "p", status: "pending" }],
      total: 1,
    });
    render(
      <MemoryRouter>
        <QubaxPanel />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByTestId("qubax-prompt")).toBeInTheDocument());
    fireEvent.change(screen.getByTestId("qubax-prompt"), { target: { value: "test prompt" } });
    fireEvent.click(screen.getByRole("button", { name: /Добавить вариант/i }));
    fireEvent.click(screen.getByTestId("qubax-batch"));
    await waitFor(() => expect(apiMock.qubax.createBatch).toHaveBeenCalled());
  });

  function renderConfigured() {
    apiMock.qubax.status.mockResolvedValue({
      configured: true,
      key_from_env: false,
      balance: 5,
    });
    apiMock.qubax.models.mockResolvedValue({ presets: [], models: [] });
    render(
      <MemoryRouter>
        <QubaxPanel />
      </MemoryRouter>
    );
  }

  it("shows a live queue status and elapsed time for an active job", async () => {
    apiMock.qubax.listJobs.mockResolvedValue({
      items: [
        {
          id: 4,
          external_id: "e",
          model: "kling",
          prompt: "forest",
          status: "pending",
          created_at: new Date(Date.now() - 70_000).toISOString(),
        },
      ],
      total: 1,
    });
    renderConfigured();
    expect(await screen.findByText("Сейчас в работе: 1")).toBeInTheDocument();
    expect(screen.getByText("В очереди")).toBeInTheDocument();
    expect(screen.getByText(/1:\d{2}/)).toBeInTheDocument();
    expect(screen.getByTestId("qubax-job-pulse")).toBeInTheDocument();
  });

  it("shows a busy label while a generation is being sent", async () => {
    let releaseJob: (value: unknown) => void = () => {};
    apiMock.qubax.listJobs.mockResolvedValue({ items: [], total: 0 });
    apiMock.qubax.createJob.mockImplementation(
      () =>
        new Promise((resolve) => {
          releaseJob = resolve;
        })
    );
    renderConfigured();
    await screen.findByTestId("qubax-prompt");
    fireEvent.change(screen.getByTestId("qubax-prompt"), { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Сгенерировать" }));
    expect(await screen.findByRole("button", { name: "Отправляю…" })).toHaveAttribute("aria-busy", "true");
    releaseJob({
      id: 9,
      external_id: "new",
      model: "kling",
      prompt: "hello",
      status: "processing",
      created_at: new Date().toISOString(),
    });
    await waitFor(() => expect(screen.getByTestId("qubax-job-9")).toHaveAttribute("data-fresh", "true"));
    expect(screen.getByText("Рендер")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Сгенерировать" })).toBeEnabled();
  });

  it("shows busy labels on refresh and library buttons", async () => {
    let releaseList: (value: unknown) => void = () => {};
    let releaseImport: (value: unknown) => void = () => {};
    const ready = {
      id: 8,
      external_id: "done",
      model: "kling",
      prompt: "ready",
      status: "completed",
      video_url: "https://cdn.example/ready.mp4",
    };
    let listCalls = 0;
    apiMock.qubax.listJobs.mockImplementation(() => {
      listCalls += 1;
      if (listCalls === 1) return Promise.resolve({ items: [ready], total: 1 });
      return new Promise((resolve) => {
        releaseList = resolve;
      });
    });
    apiMock.qubax.importJob.mockImplementation(
      () =>
        new Promise((resolve) => {
          releaseImport = resolve;
        })
    );
    renderConfigured();
    await screen.findByTestId("qubax-job-8");
    fireEvent.click(screen.getByRole("button", { name: "В библиотеку" }));
    expect(await screen.findByRole("button", { name: "Сохраняю…" })).toHaveAttribute("aria-busy", "true");
    releaseImport({ job_id: 8, media_asset_id: 3, filename: "ready.mp4" });
    await waitFor(() => expect(screen.getByRole("button", { name: "В библиотеку" })).toBeEnabled());

    fireEvent.click(screen.getByTestId("qubax-refresh-jobs"));
    expect(await screen.findByRole("button", { name: "Обновляю…" })).toHaveAttribute("aria-busy", "true");
    releaseList({ items: [ready], total: 1 });
    await waitFor(() => expect(screen.getByTestId("qubax-refresh-jobs")).toHaveTextContent("Обновить"));
  });

  it("shows a completed job as a video in the task gallery", async () => {
    apiMock.qubax.status.mockResolvedValue({
      configured: true,
      key_from_env: false,
      balance: 1,
    });
    apiMock.qubax.models.mockResolvedValue({ presets: [], models: [] });
    apiMock.qubax.listJobs.mockResolvedValue({
      items: [
        {
          id: 7,
          external_id: "ext",
          model: "kling-v3-standard-text-to-video",
          prompt: "forest",
          status: "completed",
          video_url: "https://cdn.example/v.mp4",
        },
      ],
      total: 1,
    });
    render(
      <MemoryRouter>
        <QubaxPanel />
      </MemoryRouter>
    );
    const card = await screen.findByTestId("qubax-job-7");
    expect(card.querySelector("video")).toHaveAttribute("src", "https://cdn.example/v.mp4");
  });

  it("apply suggested public url fills input", async () => {
    apiMock.qubax.status.mockResolvedValue({
      configured: true,
      key_from_env: false,
      balance: 5,
      settings: {
        api_public_base_url: "",
        api_public_source: "none",
        api_public_configured: false,
        suggested_api_public_base_url: "https://panel.example.com",
        default_model: "video-gen",
        default_model_i2v: "uncensored-i2v-pro",
      },
    });
    apiMock.qubax.models.mockResolvedValue({ presets: [], models: [] });
    apiMock.qubax.marketPrices.mockResolvedValue({ prices: {} });
    render(
      <MemoryRouter>
        <QubaxPanel />
      </MemoryRouter>
    );
    await waitFor(() => expect(screen.getByTestId("qubax-apply-public-url")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("qubax-apply-public-url"));
    expect((screen.getByTestId("qubax-public-url") as HTMLInputElement).value).toBe(
      "https://panel.example.com"
    );
  });
});
